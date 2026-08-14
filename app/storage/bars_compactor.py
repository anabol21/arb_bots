"""Crash-safe, one-output-at-a-time compaction for immutable 5-minute bars.

The collector remains the only producer for ``/data/bars/bar_5m``.  This
module materializes a separate compacted hive and never deletes a source batch
until the transfer manifest proves that the corresponding output is remote sent.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from app.schema.lean_event import LEAN_BAR_5M_BODY_COLS
from app.schema.parquet_layout import (
    BAR_5M_COMPACTED_LAYOUT_VERSION,
    BAR_5M_COMPACTED_ROOT,
    BAR_5M_SOURCE_ROOT,
    bar_5m_compacted_path,
)

# v1's archive remains read-only at /data/bars_archive.  Keep v2 recovery
# material next to its isolated output/state tree.
DEFAULT_ARCHIVE_ROOT = Path("/data/bars_compacted_v2/archive/bar_5m")
DEFAULT_WINDOW_SECONDS = 3600
DEFAULT_GRACE_SECONDS = 600
DEFAULT_RETENTION_HOURS = 168.0
DEFAULT_LOCK_PATH = Path("/run/spread-bars-compactor.lock")


class OutputIdentityCollision(ValueError):
    """A final path exists but does not represent this frozen output."""


@dataclass(frozen=True)
class BarsCompactorConfig:
    source_root: Path = BAR_5M_SOURCE_ROOT
    output_root: Path = BAR_5M_COMPACTED_ROOT
    archive_root: Path = DEFAULT_ARCHIVE_ROOT
    window_seconds: int = DEFAULT_WINDOW_SECONDS
    grace_seconds: int = DEFAULT_GRACE_SECONDS
    retention_hours: float = DEFAULT_RETENTION_HOURS
    lock_path: Path = DEFAULT_LOCK_PATH
    lookback_days: int | None = None

    def __post_init__(self) -> None:
        if self.window_seconds != DEFAULT_WINDOW_SECONDS:
            raise ValueError("bar compactor currently supports exact one-hour windows")
        if self.grace_seconds < 0 or self.retention_hours < 0:
            raise ValueError("grace and retention must be non-negative")
        if self.lookback_days is not None and self.lookback_days < 0:
            raise ValueError("lookback_days must be non-negative when set")
        for name in ("source_root", "output_root", "archive_root", "lock_path"):
            if not getattr(self, name).is_absolute():
                raise ValueError(f"{name} must be absolute")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    _fsync_dir(path.parent)


def _stamp(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _partition_parts(path: Path, source_root: Path) -> tuple[str, str] | None:
    relative = path.relative_to(source_root)
    if len(relative.parts) != 3:
        return None
    coin_part, date_part, _name = relative.parts
    if not coin_part.startswith("base_coin=") or not date_part.startswith("event_date="):
        return None
    return coin_part.split("=", 1)[1], date_part.split("=", 1)[1]


def _source_record(path: Path, root: Path) -> tuple[dict[str, Any], int] | None:
    """Return an immutable source snapshot and its only eligible hour."""
    parquet = pq.ParquetFile(path)
    names = parquet.schema_arrow.names
    if list(names) != list(LEAN_BAR_5M_BODY_COLS):
        raise ValueError(f"unexpected bar schema in {path}: {names}")
    starts = parquet.read(columns=["bar_start_ts_ms"]).column(0).to_pylist()
    if not starts:
        return None
    windows = {
        (int(value) // 1000 // DEFAULT_WINDOW_SECONDS) * DEFAULT_WINDOW_SECONDS
        for value in starts
    }
    if len(windows) != 1:
        return None
    stat = path.stat()
    return (
        {
            "path": path.relative_to(root).as_posix(),
            "bytes": stat.st_size,
            "rows": int(parquet.metadata.num_rows),
            "sha256": _sha256(path),
        },
        next(iter(windows)),
    )


def _manifest_path(config: BarsCompactorConfig, coin: str, event_date: str, name: str) -> Path:
    return config.output_root / ".state" / f"base_coin={coin}" / f"event_date={event_date}" / f"{Path(name).stem}.json"


def _inputset_digest(sources: list[dict[str, Any]]) -> str:
    """Stable identity for the exact source snapshot, not for its hour alone."""
    payload = json.dumps(sources, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()[:16]


def _load_manifest(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict) or value.get("layout_version") not in {1, BAR_5M_COMPACTED_LAYOUT_VERSION}:
        raise ValueError(f"invalid bar compaction manifest: {path}")
    return value


def _window_manifest(
    config: BarsCompactorConfig, coin: str, event_date: str, window_start: int
) -> tuple[Path, dict[str, Any]] | None:
    """Find an existing ownership record for this coin-hour.

    A terminal manifest is authoritative even if a late source batch arrives:
    silently adding it would create a second model-visible output for one hour.
    """
    directory = config.output_root / ".state" / f"base_coin={coin}" / f"event_date={event_date}"
    if not directory.is_dir():
        return None
    for path in sorted(directory.glob("bar_5m_*.json")):
        manifest = _load_manifest(path)
        if int(manifest.get("window_start_epoch_s", -1)) == window_start:
            return path, manifest
    return None


def _find_candidate(
    config: BarsCompactorConfig, now: float
) -> tuple[
    str, str, int, list[dict[str, Any]], list[tuple[dict[str, Any], str]]
] | None:
    if not config.source_root.exists():
        return None
    dates = (
        [None]
        if config.lookback_days is None
        else [
            (datetime.fromtimestamp(now, timezone.utc).date() - timedelta(days=offset)).isoformat()
            for offset in range(config.lookback_days + 1)
        ]
    )
    # Deliberately inspect only one coin/day partition per invocation.  A full
    # hive scan cost ~31 CPU seconds on the VPS and defeated the one-output
    # bound; this makes discovery bounded by one partition's batches instead.
    for coin_dir in sorted(config.source_root.glob("base_coin=*")):
        coin = coin_dir.name.split("=", 1)[1]
        date_dirs = (
            sorted(coin_dir.glob("event_date=*"))
            if dates == [None]
            else [coin_dir / f"event_date={date}" for date in sorted(dates)]
        )
        for partition in date_dirs:
            if not partition.is_dir():
                continue
            grouped: dict[int, list[dict[str, Any]]] = {}
            for source in sorted(partition.glob("batch_*.parquet")):
                record_window = _source_record(source, config.source_root)
                if record_window is None:
                    continue
                record, window_start = record_window
                # A 10-minute mtime grace alone is insufficient: it allows
                # partial current-hour output. The entire UTC window must be
                # closed, and the source must itself have remained unchanged
                # through the grace boundary.
                if now < window_start + config.window_seconds + config.grace_seconds:
                    continue
                if source.stat().st_mtime > window_start + config.window_seconds + config.grace_seconds:
                    continue
                grouped.setdefault(window_start, []).append(record)
            if grouped:
                event_date = partition.name.split("=", 1)[1]
                for window_start in sorted(grouped):
                    existing = _window_manifest(config, coin, event_date, window_start)
                    if existing is None:
                        return coin, event_date, window_start, grouped[window_start], []
                    _, manifest = existing
                    known = {
                        str(source["path"]): source for source in manifest["sources"]
                    }
                    late: list[tuple[dict[str, Any], str]] = []
                    for source in grouped[window_start]:
                        frozen = known.get(str(source["path"]))
                        if frozen is None:
                            late.append((source, "late_source_for_frozen_window"))
                        elif any(
                            source[field] != frozen.get(field)
                            for field in ("bytes", "rows", "sha256")
                        ):
                            late.append(
                                (source, "changed_source_for_frozen_window")
                            )
                        else:
                            # A source that returns after the frozen copy was
                            # archived is still an ownership violation.  Do
                            # not silently accept or archive it a second time.
                            late.append(
                                (source, "reappeared_source_for_frozen_window")
                            )
                    if late:
                        return coin, event_date, window_start, [], late
    return None


def _validate_output(path: Path, expected_rows: int) -> tuple[int, str]:
    parquet = pq.ParquetFile(path)
    if list(parquet.schema_arrow.names) != list(LEAN_BAR_5M_BODY_COLS):
        raise ValueError(f"unexpected compacted bar schema: {path}")
    rows = int(parquet.metadata.num_rows)
    if rows != expected_rows:
        raise ValueError(f"row count mismatch {path}: expected={expected_rows} actual={rows}")
    return rows, _sha256(path)


def _write_output(config: BarsCompactorConfig, manifest: dict[str, Any], final_path: Path) -> None:
    temporary = final_path.with_name(f".{final_path.name}.{os.getpid()}.inprogress")
    temporary.parent.mkdir(parents=True, exist_ok=True)
    temporary.unlink(missing_ok=True)
    writer: pq.ParquetWriter | None = None
    rows = 0
    try:
        for record in manifest["sources"]:
            source = config.source_root / str(record["path"])
            if not source.is_file() or _sha256(source) != record["sha256"]:
                raise ValueError(f"source changed or missing: {source}")
            parquet = pq.ParquetFile(source)
            if int(parquet.metadata.num_rows) != int(record["rows"]):
                raise ValueError(f"source row count changed: {source}")
            for batch in parquet.iter_batches():
                if writer is None:
                    writer = pq.ParquetWriter(str(temporary), batch.schema, compression="zstd")
                writer.write_batch(batch)
                rows += batch.num_rows
    except Exception:
        if writer is not None:
            writer.close()
        temporary.unlink(missing_ok=True)
        raise
    if writer is None:
        raise ValueError("refusing empty compacted output")
    writer.close()
    _fsync_file(temporary)
    _, checksum = _validate_output(temporary, int(manifest["rows"]))
    if final_path.exists():
        _, existing_checksum = _validate_output(final_path, int(manifest["rows"]))
        temporary.unlink()
        if existing_checksum != checksum:
            raise OutputIdentityCollision(
                f"output identity collision: {final_path} has a different checksum"
            )
    else:
        os.replace(temporary, final_path)
        _fsync_dir(final_path.parent)
    manifest["output_sha256"] = checksum
    manifest["output_bytes"] = final_path.stat().st_size


def _archive_sources(config: BarsCompactorConfig, manifest: dict[str, Any]) -> None:
    for record in manifest["sources"]:
        relative = Path(str(record["path"]))
        source = config.source_root / relative
        archive = config.archive_root / relative
        if archive.exists():
            if source.exists():
                raise ValueError(f"source and archive both exist: {relative}")
            continue
        if not source.exists():
            raise FileNotFoundError(f"source disappeared before archive: {source}")
        if _sha256(source) != record["sha256"]:
            raise ValueError(f"source changed before archive: {source}")
        archive.parent.mkdir(parents=True, exist_ok=True)
        os.replace(source, archive)
        _fsync_file(archive)
        _fsync_dir(source.parent)
        _fsync_dir(archive.parent)


def _remote_sent(config: BarsCompactorConfig, output_relative: str) -> bool:
    database = config.output_root / ".state" / "backup_manifest.sqlite3"
    if not database.is_file():
        return False
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        row = connection.execute(
            "SELECT state FROM transfers WHERE filename = ?", (output_relative,)
        ).fetchone()
        return row is not None and row[0] == "sent"
    finally:
        connection.close()


def _prune_archives(config: BarsCompactorConfig, now: float) -> int:
    removed = 0
    cutoff = now - config.retention_hours * 3600
    state_root = config.output_root / ".state"
    for path in state_root.rglob("bar_5m_*.json"):
        manifest = _load_manifest(path)
        if manifest.get("status") != "archived":
            continue
        output = str(manifest["output_relative"])
        if not _remote_sent(config, output):
            continue
        for record in manifest["sources"]:
            archived = config.archive_root / str(record["path"])
            if archived.is_file() and archived.stat().st_mtime < cutoff:
                archived.unlink()
                removed += 1
        manifest["status"] = "remote_retained"
        manifest["remote_confirmed_at"] = now
        _atomic_json(path, manifest)
    return removed


def _resume_incomplete(config: BarsCompactorConfig) -> tuple[Path, dict[str, Any]] | None:
    """Return the oldest unfinalized manifest before discovering new source work."""
    state_root = config.output_root / ".state"
    candidates: list[tuple[Path, dict[str, Any]]] = []
    if not state_root.exists():
        return None
    for path in state_root.rglob("bar_5m_*.json"):
        manifest = _load_manifest(path)
        if manifest.get("status") in {"planned", "published"}:
            candidates.append((path, manifest))
    return min(candidates, key=lambda item: str(item[0])) if candidates else None


def _materialize_manifest(
    config: BarsCompactorConfig, state: Path, manifest: dict[str, Any]
) -> dict[str, Any]:
    output = config.output_root / str(manifest["output_relative"])
    if manifest["status"] == "planned":
        # Build-and-compare also recovers a crash after final rename but before
        # the manifest update. It never replaces an existing final.
        _write_output(config, manifest, output)
        manifest["status"] = "published"
        _atomic_json(state, manifest)
    _validate_output(output, int(manifest["rows"]))
    expected_checksum = manifest.get("output_sha256")
    if not isinstance(expected_checksum, str) or _sha256(output) != expected_checksum:
        raise ValueError(f"output checksum mismatch for frozen manifest: {output}")
    if manifest["status"] == "published":
        _archive_sources(config, manifest)
        manifest["status"] = "archived"
        _atomic_json(state, manifest)
    return manifest


def _quarantine_late_sources(
    config: BarsCompactorConfig,
    coin: str,
    event_date: str,
    window_start: int,
    sources: list[tuple[dict[str, Any], str]],
) -> None:
    """Record late closed-window input without moving or compacting it."""
    for source, reason in sources:
        key = hashlib.sha256(
            f"{source['path']}:{source['sha256']}:{reason}".encode()
        ).hexdigest()[:16]
        path = (
            config.output_root
            / ".state"
            / "quarantine"
            / f"base_coin={coin}"
            / f"event_date={event_date}"
            / f"late_window={_stamp(window_start)}_{key}.json"
        )
        _atomic_json(
            path,
            {
                "layout_version": BAR_5M_COMPACTED_LAYOUT_VERSION,
                "status": "quarantined",
                "reason": reason,
                "base_coin": coin,
                "event_date": event_date,
                "window_start_epoch_s": window_start,
                "source": source,
            },
        )


def compact_once(config: BarsCompactorConfig, *, now: float | None = None) -> dict[str, Any]:
    """Compact at most one closed coin-hour; safe to call from a frequent timer."""
    now = time.time() if now is None else now
    config.output_root.mkdir(parents=True, exist_ok=True)
    resumed = _resume_incomplete(config)
    if resumed is not None:
        state, manifest = resumed
        if manifest["layout_version"] != BAR_5M_COMPACTED_LAYOUT_VERSION:
            # v1 named outputs solely by hour and cannot prove a one-to-one
            # relationship between its output and frozen inputs. Leave every
            # artifact in place; make the conflict explicit for an operator.
            manifest["status"] = "quarantined"
            manifest["quarantine_reason"] = "legacy_v1_output_identity_not_recoverable"
            _atomic_json(state, manifest)
            return {
                "event": "bars_compaction_quarantined_legacy_manifest",
                "outputs": 0,
                "manifest": str(state.relative_to(config.output_root)),
            }
    else:
        candidate = _find_candidate(config, now)
        if candidate is None:
            return {"event": "bars_compaction_idle", "outputs": 0, "archives_pruned": _prune_archives(config, now)}
        coin, event_date, window_start, sources, late_sources = candidate
        if late_sources:
            _quarantine_late_sources(
                config, coin, event_date, window_start, late_sources
            )
            return {
                "event": "bars_compaction_quarantined_late_sources",
                "outputs": 0,
                "base_coin": coin,
                "event_date": event_date,
                "window_start_epoch_s": window_start,
                "sources": len(late_sources),
            }
        window_end = window_start + config.window_seconds
        start_stamp, end_stamp = _stamp(window_start), _stamp(window_end)
        inputset_digest = _inputset_digest(sources)
        output = bar_5m_compacted_path(
            config.output_root,
            coin,
            event_date,
            start_stamp,
            end_stamp,
            inputset_digest,
        )
        state = _manifest_path(config, coin, event_date, output.name)
        manifest = {
            "layout_version": BAR_5M_COMPACTED_LAYOUT_VERSION,
            "status": "planned",
            "base_coin": coin,
            "event_date": event_date,
            "window_start_epoch_s": window_start,
            "window_end_epoch_s": window_end,
            "output_relative": output.relative_to(config.output_root).as_posix(),
            "inputset_sha256": inputset_digest,
            "sources": sources,
            "rows": sum(int(record["rows"]) for record in sources),
        }
        _atomic_json(state, manifest)
    try:
        manifest = _materialize_manifest(config, state, manifest)
    except OutputIdentityCollision as exc:
        # A planned manifest otherwise wins _resume_incomplete forever.  The
        # pre-existing final is never overwritten; make this terminal state
        # durable so an operator can reconcile the immutable identities.
        manifest["status"] = "quarantined"
        manifest["quarantine_reason"] = "local_output_identity_collision"
        manifest["quarantine_error"] = str(exc)
        _atomic_json(state, manifest)
        return {
            "event": "bars_compaction_quarantined_output_collision",
            "outputs": 0,
            "base_coin": manifest["base_coin"],
            "event_date": manifest["event_date"],
            "output": manifest["output_relative"],
            "manifest": str(state.relative_to(config.output_root)),
            "reason": manifest["quarantine_reason"],
        }
    return {
        "event": "bars_compaction_complete",
        "outputs": 1,
        "base_coin": manifest["base_coin"],
        "event_date": manifest["event_date"],
        "output": manifest["output_relative"],
        "rows": manifest["rows"],
        "bytes": manifest.get("output_bytes"),
        "source_files": len(manifest["sources"]),
        "archives_pruned": _prune_archives(config, now),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=BAR_5M_SOURCE_ROOT)
    parser.add_argument("--output-root", type=Path, default=BAR_5M_COMPACTED_ROOT)
    parser.add_argument("--archive-root", type=Path, default=DEFAULT_ARCHIVE_ROOT)
    parser.add_argument("--grace-seconds", type=int, default=DEFAULT_GRACE_SECONDS)
    parser.add_argument("--retention-hours", type=float, default=DEFAULT_RETENTION_HOURS)
    parser.add_argument("--lock-path", type=Path, default=DEFAULT_LOCK_PATH)
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=1,
        help="Only scan current UTC date plus this many prior dates; avoids legacy drain.",
    )
    args = parser.parse_args(argv)
    config = BarsCompactorConfig(
        source_root=args.source_root, output_root=args.output_root, archive_root=args.archive_root,
        grace_seconds=args.grace_seconds, retention_hours=args.retention_hours, lock_path=args.lock_path,
        lookback_days=args.lookback_days,
    )
    config.lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(config.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({"event": "bars_compaction_skipped_lock_held"}))
            return 0
        print(json.dumps(compact_once(config), sort_keys=True))
        return 0
    except Exception as exc:
        logging.exception("bars_compaction_failed")
        print(json.dumps({"event": "bars_compaction_failed", "error": repr(exc)}))
        return 1
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


if __name__ == "__main__":
    raise SystemExit(main())
