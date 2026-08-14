#!/usr/bin/env python3
"""Delete archived live files whose compacted outputs are already on remote/sent.

Safe reclaim helper for the vacation ENOSPC incident. Only touches
``/data/live/archived/**`` entries listed as sources of complete manifests whose
artifact is ``sent`` or ``offloaded`` (or present under compacted/sent).

Usage on VPS:
  /root/venv/bin/python validation/emergency_reclaim_archived.py --dry-run
  /root/venv/bin/python validation/emergency_reclaim_archived.py --execute
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", type=Path, default=Path("/data/live"))
    parser.add_argument("--compacted", type=Path, default=Path("/data/compacted"))
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--dry-run", action="store_true", default=True)
    args = parser.parse_args()
    if args.execute:
        args.dry_run = False

    archive_root = args.live / "archived"
    state_root = args.compacted / ".state"
    sent_root = args.compacted / "sent"
    if not state_root.is_dir():
        print("no state root", state_root)
        return 1

    candidates: set[str] = set()
    for state_path in state_root.glob("*.json"):
        manifest = json.loads(state_path.read_text(encoding="utf-8"))
        if manifest.get("status") != "complete":
            continue
        output = str(manifest.get("output") or "")
        location = manifest.get("artifact_location")
        sent_path = sent_root / output
        compacted_path = args.compacted / output
        durable_ok = (
            location in {"sent", "offloaded"}
            or sent_path.is_file()
            or compacted_path.is_file()
        )
        if not durable_ok:
            continue
        for source in manifest.get("sources") or []:
            relative = source.get("path")
            if isinstance(relative, str) and relative:
                candidates.add(relative)

    bytes_freed = 0
    removed = 0
    missing = 0
    for relative in sorted(candidates):
        path = archive_root / relative
        if not path.is_file():
            missing += 1
            continue
        size = path.stat().st_size
        if args.dry_run:
            removed += 1
            bytes_freed += size
            continue
        path.unlink()
        removed += 1
        bytes_freed += size

    mode = "dry-run" if args.dry_run else "execute"
    print(
        f"mode={mode} eligible_paths={len(candidates)} "
        f"removed_or_would_remove={removed} missing={missing} "
        f"bytes≈{bytes_freed} ({bytes_freed / (1024**3):.2f} GiB)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
