from __future__ import annotations

import json
import os
import sys
from pathlib import Path


MOUNT_PATH = Path("/mnt/storage")


def get_mount_line(target: Path) -> str | None:
    try:
        with open("/proc/mounts", "r", encoding="utf-8") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2 and parts[1] == str(target):
                    return line.strip()
    except FileNotFoundError:
        return None
    return None


def main() -> int:
    result: dict[str, object] = {
        "path": str(MOUNT_PATH),
        "exists": MOUNT_PATH.exists(),
        "is_dir": MOUNT_PATH.is_dir(),
    }

    try:
        result["is_mount"] = MOUNT_PATH.is_mount()
    except Exception as e:
        result["is_mount_error"] = repr(e)

    try:
        stat = os.statvfs(MOUNT_PATH)
        result["statvfs_ok"] = True
        result["f_bsize"] = stat.f_bsize
        result["f_blocks"] = stat.f_blocks
        result["f_bavail"] = stat.f_bavail
        result["free_bytes_estimate"] = stat.f_bavail * stat.f_frsize
    except Exception as e:
        result["statvfs_ok"] = False
        result["statvfs_error"] = repr(e)

    mount_line = get_mount_line(MOUNT_PATH)
    result["mount_entry_found"] = mount_line is not None
    if mount_line is not None:
        result["mount_entry"] = mount_line

    test_file = MOUNT_PATH / ".mount_write_test"
    try:
        test_file.write_text("ok\n", encoding="utf-8")
        content = test_file.read_text(encoding="utf-8").strip()
        result["write_read_ok"] = content == "ok"
        result["test_file"] = str(test_file)
        try:
            test_file.unlink()
            result["cleanup_ok"] = True
        except Exception as e:
            result["cleanup_ok"] = False
            result["cleanup_error"] = repr(e)
    except Exception as e:
        result["write_read_ok"] = False
        result["write_read_error"] = repr(e)

    print(json.dumps(result, ensure_ascii=False, indent=2))

    if not result.get("exists"):
        return 2
    if not result.get("is_dir"):
        return 3
    if not result.get("statvfs_ok"):
        return 4
    if not result.get("write_read_ok"):
        return 5

    return 0


if __name__ == "__main__":
    sys.exit(main())