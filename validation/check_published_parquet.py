from __future__ import annotations

import json
import time
from pathlib import Path

import pyarrow.parquet as pq

from app.storage.paths import resolve_parquet_root

PARQUET_ROOT = resolve_parquet_root()
TMP_DIR = PARQUET_ROOT / ".tmp"


def collect_parquet_stats(root: Path) -> dict[str, object]:
    now = time.time()
    files = sorted(root.rglob("*.parquet")) if root.exists() else []
    # Live accounting excludes temporary and already-compacted source archives.
    files = [
        p
        for p in files
        if ".tmp" not in p.parts and "archived" not in p.parts
    ]

    total_bytes = 0
    oldest_age_s = None
    newest_age_s = None
    readable = 0
    unreadable: list[str] = []
    sample_files: list[str] = []

    for i, path in enumerate(files):
        try:
            st = path.stat()
        except FileNotFoundError:
            continue
        total_bytes += st.st_size
        age_s = now - st.st_mtime
        oldest_age_s = age_s if oldest_age_s is None else max(oldest_age_s, age_s)
        newest_age_s = age_s if newest_age_s is None else min(newest_age_s, age_s)
        if i < 10:
            sample_files.append(str(path.relative_to(root)))
        try:
            meta = pq.ParquetFile(path).metadata
            if meta is None or meta.num_rows < 0:
                raise ValueError("missing parquet metadata")
            readable += 1
        except Exception as exc:
            unreadable.append(f"{path}: {exc!r}")

    return {
        "exists": root.exists(),
        "path": str(root),
        "count": len(files),
        "readable_count": readable,
        "unreadable_count": len(unreadable),
        "unreadable_sample": unreadable[:5],
        "bytes": total_bytes,
        "oldest_age_s": None if oldest_age_s is None else round(oldest_age_s, 3),
        "newest_age_s": None if newest_age_s is None else round(newest_age_s, 3),
        "sample_files": sample_files,
    }


def collect_tmp_stats(tmp_dir: Path) -> dict[str, object]:
    tmps = sorted(tmp_dir.glob("*.parquet.tmp")) if tmp_dir.exists() else []
    return {
        "exists": tmp_dir.exists(),
        "path": str(tmp_dir),
        "tmp_count": len(tmps),
        "sample_files": [p.name for p in tmps[:10]],
    }


def main() -> int:
    result = {
        "design": "local_primary",
        "published": collect_parquet_stats(PARQUET_ROOT),
        "tmp": collect_tmp_stats(TMP_DIR),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))

    published = result["published"]
    if not published["exists"]:
        return 2
    if published["unreadable_count"]:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
