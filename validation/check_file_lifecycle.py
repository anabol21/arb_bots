"""Design B lifecycle checker (ready/uploading/uploaded/failed).

Design A (current) does NOT use these directories.
Use validation/check_published_parquet.py for Design A Hive publishes under
/mnt/storage/spreads_parquet_by_coins.
"""

from __future__ import annotations

import json
import time
from pathlib import Path


OUTPUT_ROOT = Path("/mnt/storage")
STATE_DIRS = {
    "ready": OUTPUT_ROOT / "ready",
    "uploading": OUTPUT_ROOT / "uploading",
    "uploaded": OUTPUT_ROOT / "uploaded",
    "failed": OUTPUT_ROOT / "failed",
}


def collect_state(path: Path) -> dict[str, object]:
    now = time.time()
    parquet_files = sorted(path.rglob("*.parquet")) if path.exists() else []
    total_bytes = 0
    oldest_age_s = None
    newest_age_s = None
    sample_files: list[str] = []

    for i, p in enumerate(parquet_files):
        try:
            st = p.stat()
        except FileNotFoundError:
            continue
        total_bytes += st.st_size
        age_s = now - st.st_mtime
        oldest_age_s = age_s if oldest_age_s is None else max(oldest_age_s, age_s)
        newest_age_s = age_s if newest_age_s is None else min(newest_age_s, age_s)
        if i < 10:
            sample_files.append(str(p.relative_to(path)))

    return {
        "exists": path.exists(),
        "path": str(path),
        "count": len(parquet_files),
        "bytes": total_bytes,
        "oldest_age_s": None if oldest_age_s is None else round(oldest_age_s, 3),
        "newest_age_s": None if newest_age_s is None else round(newest_age_s, 3),
        "sample_files": sample_files,
    }


def main() -> int:
    result = {
        "note": "Design B lifecycle dirs; not used by Design A runtime",
        "output_root": str(OUTPUT_ROOT),
        "states": {name: collect_state(path) for name, path in STATE_DIRS.items()},
    }

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
