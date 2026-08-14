"""Read-only backup validity check: remote inventory vs local manifests/sent.

Compares rclone remote sizes to local compacted/sent + transfer sqlite, then
optionally re-downloads files to verify SHA-256 and parquet row counts.

After sent/ retention, local copies may be gone. In that case inventory treats
remote+transfer(+manifest size when known) as durable, and --download verifies
remote bytes against manifest output_sha256 / total_rows without requiring local.

Does not delete remote data. Downloads land under --verify-dir and are removed
after each file check. Safe to run while the collector/canary is up.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from app.schema.spread_event import SPREAD_EVENT_BODY_COLS

DEFAULT_RCLONE = "/opt/rclone-1.74.4/rclone"
DEFAULT_KEY = "/root/.ssh/id_ed25519_uploader"

# Writer body columns (event_date is hive-partition only, not in file body).
EXPECTED_BODY_COLS = list(SPREAD_EVENT_BODY_COLS)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def rclone_lsl(
    *,
    rclone: str,
    remote_prefix: str,
    key: str,
) -> dict[str, int]:
    command = [
        rclone,
        "lsl",
        remote_prefix,
        "--timeout",
        "90s",
        "--retries",
        "1",
        "--contimeout",
        "30s",
        "--sftp-key-file",
        key,
    ]
    proc = subprocess.run(command, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"rclone lsl failed for {remote_prefix}: {proc.stderr[-800:]}")
    listing: dict[str, int] = {}
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        name = parts[-1]
        if name.endswith(".parquet"):
            listing[name] = int(parts[0])
    return listing


def download_one(
    *,
    rclone: str,
    remote_prefix: str,
    name: str,
    dest: Path,
    key: str,
) -> None:
    if dest.exists():
        dest.unlink()
    command = [
        rclone,
        "copyto",
        f"{remote_prefix}/{name}",
        str(dest),
        "--timeout",
        "300s",
        "--retries",
        "2",
        "--contimeout",
        "30s",
        "--sftp-key-file",
        key,
    ]
    proc = subprocess.run(command, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"download failed for {name}: {proc.stderr[-800:]}")


def load_manifests(state: Path) -> dict[str, dict[str, Any]]:
    manifests: dict[str, dict[str, Any]] = {}
    for path in sorted(state.glob("spread_*.json")):
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if manifest.get("status") != "complete":
            continue
        manifests[str(manifest["output"])] = manifest
    return manifests


def load_transfers(sqlite_path: Path) -> list[dict[str, Any]]:
    if not sqlite_path.is_file():
        return []
    connection = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT filename, size, state, remote_path FROM transfers ORDER BY filename"
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def find_local(compacted: Path, name: str) -> Path | None:
    for candidate in (compacted / "sent" / name, compacted / name):
        if candidate.is_file():
            return candidate
    return None


def _manifest_bytes(manifest: dict[str, Any] | None) -> int | None:
    if manifest is None:
        return None
    for key in ("output_bytes", "bytes", "size"):
        if key in manifest and manifest[key] is not None:
            return int(manifest[key])
    return None


def _select_verify_names(
    candidates: list[str],
    *,
    sample: int | None,
    seed: int,
) -> list[str]:
    if not candidates:
        return []
    ordered = sorted(candidates)
    if sample is None or sample <= 0 or sample >= len(ordered):
        return ordered
    # Deterministic stratified sample: first, middle, last, plus evenly spaced.
    if sample == 1:
        return [ordered[0]]
    if sample == 2:
        return [ordered[0], ordered[-1]]
    picks: list[str] = [ordered[0], ordered[len(ordered) // 2], ordered[-1]]
    remaining = sample - len(picks)
    if remaining <= 0:
        # Deduplicate while preserving order.
        seen: set[str] = set()
        out: list[str] = []
        for name in picks:
            if name not in seen:
                seen.add(name)
                out.append(name)
        return out[:sample]
    step = max(1, len(ordered) // (remaining + 1))
    idx = (seed % max(1, step)) + step
    while len(picks) < sample and idx < len(ordered) - 1:
        picks.append(ordered[idx])
        idx += step
    # Fill from start if still short (small N edge cases).
    for name in ordered:
        if len(picks) >= sample:
            break
        if name not in picks:
            picks.append(name)
    seen = set()
    out = []
    for name in picks:
        if name not in seen:
            seen.add(name)
            out.append(name)
    return out[:sample]


def validate_dataset(
    *,
    label: str,
    compacted: Path,
    remote_prefix: str,
    rclone: str,
    key: str,
    verify_dir: Path,
    download: bool,
    sample: int | None = None,
    seed: int = 42,
) -> dict[str, Any]:
    state = compacted / ".state"
    manifests = load_manifests(state)
    transfers = load_transfers(state / "backup_manifest.sqlite3")
    remote_files = rclone_lsl(rclone=rclone, remote_prefix=remote_prefix, key=key)

    transfer_sent = [
        row
        for row in transfers
        if row["state"] in ("sent", "confirmed") and str(row["filename"]).startswith("spread_20")
    ]
    # Prefer canary/prod windows that have complete manifests. Transfer sqlite may
    # include older smoke files not on this remote prefix.
    expected_names = set(manifests) | {
        row["filename"] for row in transfer_sent if row["filename"] in manifests
    }
    # Also include transfer-sent spread files that exist on remote even without
    # a local manifest (should be rare for this check).
    for row in transfer_sent:
        name = str(row["filename"])
        if name in remote_files:
            expected_names.add(name)

    remote_size_ok: list[dict[str, Any]] = []
    remote_size_bad: list[dict[str, Any]] = []
    missing_remote: list[str] = []
    missing_local_retained: list[str] = []
    local_present_names: list[str] = []
    extra_remote: list[dict[str, Any]] = []
    transfer_only_not_on_remote: list[str] = []

    for name in sorted(expected_names):
        local = find_local(compacted, name)
        remote_size = remote_files.get(name)
        transfer = next((row for row in transfers if row["filename"] == name), None)
        manifest = manifests.get(name)
        local_size = None if local is None else local.stat().st_size
        transfer_size = None if transfer is None else int(transfer["size"])
        manifest_bytes = _manifest_bytes(manifest)

        # Durability for integrity: remote present + size agrees with transfer
        # and/or local when available. Local absence after sent/ retention is OK.
        size_refs = [s for s in (transfer_size, local_size, manifest_bytes) if s is not None]
        remote_ok = remote_size is not None and (
            not size_refs or all(remote_size == s for s in size_refs)
        )
        entry = {
            "filename": name,
            "local_present": local is not None,
            "remote_present": remote_size is not None,
            "local_size": local_size,
            "remote_size": remote_size,
            "transfer_size": transfer_size,
            "manifest_bytes": manifest_bytes,
            "transfer_state": None if transfer is None else transfer["state"],
            "manifest_rows": None if manifest is None else int(manifest["total_rows"]),
            "remote_size_ok": bool(remote_ok),
            "retained_away": local is None and remote_size is not None,
        }
        if local is None and remote_size is not None:
            missing_local_retained.append(name)
        elif local is not None:
            local_present_names.append(name)
        if remote_size is None:
            missing_remote.append(name)
            if transfer is not None and transfer["state"] in ("sent", "confirmed"):
                transfer_only_not_on_remote.append(name)
        if remote_ok:
            remote_size_ok.append(entry)
        else:
            remote_size_bad.append(entry)

    for name, size in sorted(remote_files.items()):
        if name not in expected_names and name.startswith("spread_20"):
            extra_remote.append({"filename": name, "remote_size": size})

    download_results: list[dict[str, Any]] = []
    dataset_verify = verify_dir / label
    dataset_verify.mkdir(parents=True, exist_ok=True)

    verify_candidates = [
        entry["filename"]
        for entry in remote_size_ok
        if entry["filename"] in manifests and entry["remote_present"]
    ]
    verify_names = _select_verify_names(verify_candidates, sample=sample, seed=seed)

    if download:
        for name in verify_names:
            local = find_local(compacted, name)
            manifest = manifests[name]
            dest = dataset_verify / name
            started = time.time()
            try:
                download_one(
                    rclone=rclone,
                    remote_prefix=remote_prefix,
                    name=name,
                    dest=dest,
                    key=key,
                )
                remote_sha = sha256_file(dest)
                manifest_sha = manifest.get("output_sha256")
                remote_rows = int(pq.ParquetFile(dest).metadata.num_rows)
                schema_names = list(pq.ParquetFile(dest).schema_arrow.names)
                missing_cols = [col for col in EXPECTED_BODY_COLS if col not in schema_names]
                local_sha = None
                local_rows = None
                if local is not None:
                    local_sha = sha256_file(local)
                    local_rows = int(pq.ParquetFile(local).metadata.num_rows)
                sha_match = remote_sha == manifest_sha and (
                    local_sha is None or remote_sha == local_sha
                )
                row_match = remote_rows == int(manifest["total_rows"]) and (
                    local_rows is None or remote_rows == local_rows
                )
                ok = sha_match and row_match and not missing_cols
                result = {
                    "filename": name,
                    "ok": ok,
                    "mode": "local+remote+manifest" if local is not None else "remote+manifest",
                    "sha_match": sha_match,
                    "row_match": row_match,
                    "remote_sha256": remote_sha,
                    "manifest_sha256": manifest_sha,
                    "local_sha256": local_sha,
                    "remote_rows": remote_rows,
                    "local_rows": local_rows,
                    "manifest_rows": int(manifest["total_rows"]),
                    "missing_cols": missing_cols,
                    "bytes": dest.stat().st_size,
                    "local_present": local is not None,
                    "download_s": round(time.time() - started, 3),
                }
            except Exception as exc:  # noqa: BLE001 - operator evidence path
                result = {"filename": name, "ok": False, "error": repr(exc)}
            finally:
                if dest.exists():
                    dest.unlink()
            download_results.append(result)

    downloads_failed = [row for row in download_results if not row.get("ok")]
    remote_bytes_ok = sum(int(e["remote_size"] or 0) for e in remote_size_ok)
    verified_bytes = sum(int(row.get("bytes") or 0) for row in download_results if row.get("ok"))
    summary = {
        "label": label,
        "compacted": str(compacted),
        "remote_prefix": remote_prefix,
        "remote_file_count": len(remote_files),
        "remote_total_bytes": sum(remote_files.values()),
        "transfer_sent_count": len(transfer_sent),
        "manifest_complete_count": len(manifests),
        "expected_names_count": len(expected_names),
        "remote_size_ok_count": len(remote_size_ok),
        "remote_size_bad_count": len(remote_size_bad),
        "remote_bytes_ok": remote_bytes_ok,
        "missing_remote": missing_remote,
        "missing_local_retained": missing_local_retained,
        "missing_local_retained_count": len(missing_local_retained),
        "local_present_count": len(local_present_names),
        "extra_remote": extra_remote,
        "transfer_only_not_on_remote": transfer_only_not_on_remote,
        "remote_size_bad_detail": remote_size_bad,
        "verify_sample": sample,
        "verify_selected": len(verify_names),
        "downloads_checked": len(download_results),
        "downloads_ok": sum(1 for row in download_results if row.get("ok")),
        "downloads_failed": downloads_failed,
        "download_results": download_results,
        "manifest_rows_sum": sum(int(manifest["total_rows"]) for manifest in manifests.values()),
        "verified_rows_sum": sum(
            int(row.get("remote_rows") or 0) for row in download_results if row.get("ok")
        ),
        "verified_bytes_sum": verified_bytes,
        # Backward-compatible aliases used by older docs/scripts.
        "size_matched": len(remote_size_ok),
        "size_mismatched": len(remote_size_bad),
        "missing_local": missing_local_retained,
        "mismatched_detail": remote_size_bad,
    }

    # Integrity pass: every expected canary window present on remote with size OK.
    # Local absence after retention is NOT a failure.
    inventory_ok = (
        not missing_remote
        and not remote_size_bad
        and len(remote_size_ok) == len(expected_names)
    )
    if download:
        summary["pass"] = (
            inventory_ok
            and summary["downloads_checked"] > 0
            and not downloads_failed
            and summary["downloads_ok"] == summary["downloads_checked"]
        )
        summary["accounting_note"] = (
            "local sent/ retention may remove copies; remote+manifest SHA/rows are authoritative"
        )
    else:
        summary["pass"] = inventory_ok
        summary["accounting_note"] = "inventory-only; use --download for SHA/row proof"
    summary["inventory_ok"] = inventory_ok
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", required=True)
    parser.add_argument("--compacted", type=Path, required=True)
    parser.add_argument(
        "--remote-prefix",
        required=True,
        help="e.g. backup1tb:prod-soak-20260803_122023",
    )
    parser.add_argument("--rclone", default=DEFAULT_RCLONE)
    parser.add_argument("--key", default=DEFAULT_KEY)
    parser.add_argument(
        "--verify-dir",
        type=Path,
        default=Path("/tmp/backup_validity"),
    )
    parser.add_argument(
        "--download",
        action="store_true",
        help="Re-download selected remote files and verify SHA-256 + rows vs manifest",
    )
    parser.add_argument(
        "--sample",
        type=int,
        default=None,
        help="If set with --download, verify this many files (stratified). Omit for full set.",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Seed for stratified sample offset (default 42).",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Optional JSON output path",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    args.verify_dir.mkdir(parents=True, exist_ok=True)
    summary = validate_dataset(
        label=args.label,
        compacted=args.compacted,
        remote_prefix=args.remote_prefix,
        rclone=args.rclone,
        key=args.key,
        verify_dir=args.verify_dir,
        download=args.download,
        sample=args.sample,
        seed=args.seed,
    )
    payload = {
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "dataset": summary,
    }
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    print(text, end="")
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
    return 0 if summary["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
