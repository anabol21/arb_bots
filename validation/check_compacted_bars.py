"""Read-only integrity check for compacted-bars v2 and its transfer lifecycle."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from app.schema.lean_event import LEAN_BAR_5M_BODY_COLS
from app.schema.parquet_layout import (
    BAR_5M_COMPACTED_LAYOUT_VERSION,
    BAR_5M_COMPACTED_ROOT,
)

_V2_NAME = re.compile(
    r"^bar_5m_(\d{8}T\d{6}Z)_(\d{8}T\d{6}Z)_inputset=([0-9a-f]{16})\.parquet$"
)
_TERMINAL_MANIFEST_STATES = frozenset({"archived", "remote_retained", "quarantined"})


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError("manifest is not an object")
    return value


def _transfer_rows(database: Path) -> list[sqlite3.Row]:
    if not database.is_file():
        return []
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        return list(connection.execute("SELECT * FROM transfers"))
    finally:
        connection.close()


def _remote_sha256(
    relative: str,
    *,
    remote: str,
    remote_path: str,
    rclone: str,
    key_path: Path | None,
) -> str:
    target = f"{remote.rstrip(':')}:{remote_path.strip('/')}/{relative}"
    command = [rclone, "cat", target]
    if key_path is not None:
        command.extend(["--sftp-key-file", str(key_path)])
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert process.stdout is not None
    digest = hashlib.sha256()
    for chunk in iter(lambda: process.stdout.read(1024 * 1024), b""):
        digest.update(chunk)
    stderr = process.stderr.read().decode(errors="replace") if process.stderr else ""
    if process.wait() != 0:
        raise RuntimeError(f"remote read failed for {relative}: {stderr.strip()}")
    return digest.hexdigest()


def inspect_compacted_bars(
    root: Path,
    sample_limit: int = 10,
    *,
    remote: str | None = None,
    remote_path: str | None = None,
    rclone: str = "rclone",
    key_path: Path | None = None,
) -> dict[str, object]:
    active_files = (
        sorted(
            path
            for path in root.rglob("bar_5m_*.parquet")
            if ".state" not in path.parts and "sent" not in path.parts and ".tmp" not in path.parts
        )
        if root.exists()
        else []
    )
    sent_root = root / "sent"
    sent_files = sorted(sent_root.rglob("bar_5m_*.parquet")) if sent_root.exists() else []
    files = active_files + sent_files
    errors: list[str] = []
    rows = 0
    for path in files:
        try:
            if _V2_NAME.fullmatch(path.name) is None:
                raise ValueError("not a v2 inputset filename")
            parquet = pq.ParquetFile(path)
            if list(parquet.schema_arrow.names) != list(LEAN_BAR_5M_BODY_COLS):
                raise ValueError(f"unexpected columns {parquet.schema_arrow.names}")
            rows += int(parquet.metadata.num_rows)
        except Exception as exc:  # evidence command must report every bad sample
            errors.append(f"{path}: {exc!r}")
    manifests: dict[str, dict[str, Any]] = {}
    manifest_states: dict[str, int] = {}
    late_states: dict[str, int] = {}
    state_root = root / ".state"
    if state_root.exists():
        for path in state_root.rglob("bar_5m_*.json"):
            try:
                manifest = _load_json(path)
                status = str(manifest.get("status"))
                manifest_states[status] = manifest_states.get(status, 0) + 1
                relative = manifest.get("output_relative")
                if isinstance(relative, str):
                    manifests[relative] = manifest
                if manifest.get("layout_version") != BAR_5M_COMPACTED_LAYOUT_VERSION:
                    if status not in _TERMINAL_MANIFEST_STATES:
                        errors.append(f"{path}: non-terminal legacy manifest")
                    continue
                if status not in _TERMINAL_MANIFEST_STATES | {"planned", "published"}:
                    errors.append(f"{path}: unknown manifest status {status!r}")
                if status == "planned":
                    errors.append(f"{path}: pending planned manifest")
            except Exception as exc:
                errors.append(f"{path}: invalid manifest: {exc!r}")
        for path in (state_root / "quarantine").rglob("*.json") if (state_root / "quarantine").exists() else []:
            try:
                reason = str(_load_json(path).get("reason", "unknown"))
                late_states[reason] = late_states.get(reason, 0) + 1
            except Exception as exc:
                errors.append(f"{path}: invalid quarantine record: {exc!r}")

    for relative, manifest in manifests.items():
        if manifest.get("layout_version") != BAR_5M_COMPACTED_LAYOUT_VERSION:
            continue
        filename = Path(relative).name
        match = _V2_NAME.fullmatch(filename)
        if match is None:
            errors.append(f"{relative}: manifest output is not a v2 inputset filename")
            continue
        if match.group(3) != manifest.get("inputset_sha256"):
            errors.append(f"{relative}: filename inputset does not match manifest")
        status = manifest.get("status")
        # A transfer can atomically move the final to sent/ before the next
        # compactor invocation advances archived -> remote_retained. Validate
        # that legal crash/restart interval without treating it as data loss.
        active = root / relative
        retained = root / "sent" / relative
        expected = retained if status == "remote_retained" else active
        if status == "archived" and not active.exists() and retained.exists():
            expected = retained
        if status == "quarantined":
            continue
        try:
            parquet = pq.ParquetFile(expected)
            actual_rows = int(parquet.metadata.num_rows)
            actual_sha = _sha256(expected)
            if actual_rows != int(manifest["rows"]):
                errors.append(f"{relative}: manifest rows mismatch")
            if actual_sha != manifest.get("output_sha256"):
                errors.append(f"{relative}: manifest SHA-256 mismatch")
        except Exception as exc:
            errors.append(f"{relative}: expected final unavailable/invalid: {exc!r}")

    transfer_rows = _transfer_rows(state_root / "backup_manifest.sqlite3")
    transfer_states: dict[str, int] = {}
    now = time.time()
    pending_rows = [row for row in transfer_rows if row["state"] in {"pending", "failed"}]
    for row in transfer_rows:
        state = str(row["state"])
        transfer_states[state] = transfer_states.get(state, 0) + 1
        if state == "sent":
            retained = root / "sent" / str(row["filename"])
            manifest = manifests.get(str(row["filename"]))
            try:
                retained_sha = _sha256(retained)
                if retained.stat().st_size != int(row["size"]):
                    errors.append(f"{row['filename']}: retained sent size mismatch")
                if manifest is not None and retained_sha != manifest.get("output_sha256"):
                    errors.append(f"{row['filename']}: retained sent SHA-256 mismatch")
                if remote is not None and remote_path is not None:
                    remote_sha = _remote_sha256(
                        str(row["filename"]),
                        remote=remote,
                        remote_path=remote_path,
                        rclone=rclone,
                        key_path=key_path,
                    )
                    if remote_sha != retained_sha:
                        errors.append(f"{row['filename']}: retained sent/remote SHA-256 mismatch")
            except Exception as exc:
                errors.append(f"{row['filename']}: invalid retained sent copy: {exc!r}")
    pending_bytes = sum(int(row["size"]) for row in pending_rows)
    pending_oldest_age_s = (
        max(0.0, now - min(float(row["created_at"]) for row in pending_rows))
        if pending_rows
        else 0.0
    )
    return {
        "root": str(root),
        "files": len(files),
        "rows": rows,
        "readable": len(files) - len(errors),
        "errors": errors[:sample_limit],
        "sample": [str(path.relative_to(root)) for path in files[:sample_limit]],
        "manifest_states": manifest_states,
        "late_quarantine_reasons": late_states,
        "transfer_states": transfer_states,
        "terminal_conflicts": transfer_states.get("conflict", 0),
        "terminal_late_quarantines": sum(late_states.values()),
        "pending_count": len(pending_rows),
        "pending_bytes": pending_bytes,
        "pending_oldest_age_s": round(pending_oldest_age_s, 3),
        "remote_sha_verified": remote is not None and remote_path is not None,
        "model_input_contract": "bar_5m v0 body under compacted layout v2",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=BAR_5M_COMPACTED_ROOT)
    parser.add_argument("--sample-limit", type=int, default=10)
    parser.add_argument("--remote", help="Optional rclone remote name for SHA streaming verification.")
    parser.add_argument("--remote-path", help="Remote prefix; required with --remote.")
    parser.add_argument("--rclone", default="rclone")
    parser.add_argument("--key-path", type=Path)
    args = parser.parse_args()
    if bool(args.remote) != bool(args.remote_path):
        parser.error("--remote and --remote-path must be supplied together")
    result = inspect_compacted_bars(
        args.root,
        args.sample_limit,
        remote=args.remote,
        remote_path=args.remote_path,
        rclone=args.rclone,
        key_path=args.key_path,
    )
    print(json.dumps(result, sort_keys=True))
    return 0 if result["files"] > 0 and not result["errors"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
