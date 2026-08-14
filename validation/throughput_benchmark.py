#!/usr/bin/env python3
"""Isolated rclone SFTP throughput benchmark (upload + download).

Runs sequential copyto transfers under an exclusive flock so concurrent
backup_transfer invocations cannot distort the measurement.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import statistics
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path


SIZES_MIB = (5, 10, 20, 30, 50)
DEFAULT_GAP_BETWEEN_FILES_S = 150
DEFAULT_GAP_BETWEEN_SERIES_S = 2 * 3600
DEFAULT_RCLONE_TIMEOUT_S = 600


def _emit(path: Path, event: str, **fields: object) -> None:
    payload = {
        "event": event,
        "timestamp": time.time(),
        "utc": datetime.now(timezone.utc).isoformat(),
        **fields,
    }
    line = json.dumps(payload, sort_keys=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    print(line, flush=True)


def _acquire_lock(lock_path: Path) -> int:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(lock_fd, fcntl.LOCK_EX)
    return lock_fd


def _release_lock(lock_fd: int) -> None:
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
    finally:
        os.close(lock_fd)


def _make_file(path: Path, size_mib: int) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size == size_mib * 1024 * 1024:
        return path.stat().st_size
    # /dev/urandom avoids provider-side zero compression/dedup distortion.
    subprocess.run(
        [
            "dd",
            "if=/dev/urandom",
            f"of={path}",
            "bs=1M",
            f"count={size_mib}",
            "status=none",
            "conv=fsync",
        ],
        check=True,
    )
    return path.stat().st_size


def _rclone_copyto(
    *,
    rclone: str,
    key_path: Path,
    source: str,
    destination: str,
    timeout_s: int,
    extra_flags: list[str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    command = [
        rclone,
        "copyto",
        source,
        destination,
        "--timeout",
        f"{timeout_s}s",
        "--contimeout",
        "15s",
        "--retries",
        "1",
        "--sftp-key-file",
        str(key_path),
        *(extra_flags or ()),
    ]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout_s + 30,
        check=False,
    )
    return result, command


def _transfer_one(
    *,
    log_path: Path,
    series: int,
    direction: str,
    size_mib: int,
    local_path: Path,
    remote: str,
    remote_name: str,
    rclone: str,
    key_path: Path,
    lock_path: Path,
    timeout_s: int,
    extra_flags: list[str] | None = None,
) -> dict[str, object]:
    size = local_path.stat().st_size if direction == "upload" else size_mib * 1024 * 1024
    remote_spec = f"{remote.rstrip(':')}:{remote_name}"
    lock_fd = _acquire_lock(lock_path)
    command: list[str] = []
    started = time.time()
    try:
        if direction == "upload":
            result, command = _rclone_copyto(
                rclone=rclone,
                key_path=key_path,
                source=str(local_path),
                destination=remote_spec,
                timeout_s=timeout_s,
                extra_flags=extra_flags,
            )
        else:
            download_path = local_path.with_name(local_path.name + ".download")
            download_path.unlink(missing_ok=True)
            result, command = _rclone_copyto(
                rclone=rclone,
                key_path=key_path,
                source=remote_spec,
                destination=str(download_path),
                timeout_s=timeout_s,
                extra_flags=extra_flags,
            )
            if result.returncode == 0 and download_path.exists():
                size = download_path.stat().st_size
        ended = time.time()
    except subprocess.TimeoutExpired as exc:
        ended = time.time()
        command = list(exc.cmd) if exc.cmd else command
        result = subprocess.CompletedProcess(
            exc.cmd or [],
            -1,
            "",
            f"python_timeout_after_{timeout_s}s",
        )
    finally:
        _release_lock(lock_fd)

    duration = max(ended - started, 1e-9)
    throughput = (size / (1024 * 1024)) / duration
    record = {
        "series": series,
        "direction": direction,
        "size_mib": size_mib,
        "size_bytes": size,
        "start_epoch": started,
        "end_epoch": ended,
        "duration_s": round(duration, 6),
        "throughput_mib_s": round(throughput, 6),
        "returncode": result.returncode,
        "success": result.returncode == 0,
        "stderr_tail": (result.stderr or "")[-500:],
        "remote": remote_spec,
        "local": str(local_path),
        "rclone_command": command,
        "rclone_extra_flags": list(extra_flags or ()),
        "hour_utc": datetime.fromtimestamp(started, timezone.utc).strftime("%H:%M"),
    }
    _emit(log_path, "throughput_sample", **record)
    return record


def run_series(
    *,
    series: int,
    local_dir: Path,
    remote: str,
    remote_prefix: str,
    rclone: str,
    key_path: Path,
    lock_path: Path,
    log_path: Path,
    timeout_s: int,
    gap_between_files_s: int,
    include_download: bool,
    extra_flags: list[str] | None = None,
) -> list[dict[str, object]]:
    samples: list[dict[str, object]] = []
    _emit(
        log_path,
        "series_start",
        series=series,
        sizes_mib=list(SIZES_MIB),
        rclone_extra_flags=list(extra_flags or ()),
    )
    for index, size_mib in enumerate(SIZES_MIB):
        local_path = local_dir / f"bench_s{series}_{size_mib}m.bin"
        _make_file(local_path, size_mib)
        remote_name = f"{remote_prefix}/bench_s{series}_{size_mib}m.bin"
        sample = _transfer_one(
            log_path=log_path,
            series=series,
            direction="upload",
            size_mib=size_mib,
            local_path=local_path,
            remote=remote,
            remote_name=remote_name,
            rclone=rclone,
            key_path=key_path,
            lock_path=lock_path,
            timeout_s=timeout_s,
            extra_flags=extra_flags,
        )
        samples.append(sample)
        if include_download and sample["success"]:
            samples.append(
                _transfer_one(
                    log_path=log_path,
                    series=series,
                    direction="download",
                    size_mib=size_mib,
                    local_path=local_path,
                    remote=remote,
                    remote_name=remote_name,
                    rclone=rclone,
                    key_path=key_path,
                    lock_path=lock_path,
                    timeout_s=timeout_s,
                    extra_flags=extra_flags,
                )
            )
        if index + 1 < len(SIZES_MIB):
            time.sleep(gap_between_files_s)
    _emit(log_path, "series_end", series=series, samples=len(samples))
    return samples


def summarize(samples: list[dict[str, object]]) -> dict[str, object]:
    uploads = [s for s in samples if s["direction"] == "upload" and s["success"]]
    downloads = [s for s in samples if s["direction"] == "download" and s["success"]]

    def stats(values: list[float]) -> dict[str, float | None]:
        if not values:
            return {"min": None, "max": None, "mean": None, "stdev": None, "p10": None}
        ordered = sorted(values)
        p10_index = max(0, math.ceil(0.10 * len(ordered)) - 1)
        return {
            "min": round(min(values), 6),
            "max": round(max(values), 6),
            "mean": round(statistics.mean(values), 6),
            "stdev": round(statistics.stdev(values), 6) if len(values) > 1 else 0.0,
            "p10": round(ordered[p10_index], 6),
        }

    upload_rates = [float(s["throughput_mib_s"]) for s in uploads]
    download_rates = [float(s["throughput_mib_s"]) for s in downloads]
    return {
        "upload_count": len(uploads),
        "download_count": len(downloads),
        "upload_failed": sum(
            1 for s in samples if s["direction"] == "upload" and not s["success"]
        ),
        "download_failed": sum(
            1 for s in samples if s["direction"] == "download" and not s["success"]
        ),
        "upload_mib_s": stats(upload_rates),
        "download_mib_s": stats(download_rates),
        "rows": [
            {
                "series": s["series"],
                "direction": s["direction"],
                "size_mib": s["size_mib"],
                "throughput_mib_s": s["throughput_mib_s"],
                "duration_s": s["duration_s"],
                "hour_utc": s["hour_utc"],
                "success": s["success"],
            }
            for s in samples
        ],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local-dir", type=Path, required=True)
    parser.add_argument("--remote", required=True)
    parser.add_argument("--remote-prefix", required=True)
    parser.add_argument("--key-path", type=Path, required=True)
    parser.add_argument("--rclone", default="rclone")
    parser.add_argument("--lock-path", type=Path, default=Path("/run/spread-backup.lock"))
    parser.add_argument("--log-path", type=Path, required=True)
    parser.add_argument("--series-count", type=int, default=3)
    parser.add_argument(
        "--gap-between-files-s",
        type=int,
        default=DEFAULT_GAP_BETWEEN_FILES_S,
    )
    parser.add_argument(
        "--gap-between-series-s",
        type=int,
        default=DEFAULT_GAP_BETWEEN_SERIES_S,
    )
    parser.add_argument("--timeout-s", type=int, default=DEFAULT_RCLONE_TIMEOUT_S)
    parser.add_argument(
        "--download-size-mib",
        type=int,
        default=20,
        help="Download one uploaded file of this size after each series.",
    )
    parser.add_argument(
        "--rclone-extra-flag",
        action="append",
        default=[],
        metavar="TOKEN",
        help=(
            "Extra rclone CLI token. Repeatable. Prefer equals form so leading "
            "dashes are not parsed as options, e.g. "
            "--rclone-extra-flag=--sftp-concurrency --rclone-extra-flag=32"
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    extra_flags = list(args.rclone_extra_flag)
    all_samples: list[dict[str, object]] = []
    rclone_version = subprocess.run(
        [args.rclone, "version"],
        capture_output=True,
        text=True,
        check=False,
    )
    _emit(
        args.log_path,
        "benchmark_start",
        series_count=args.series_count,
        sizes_mib=list(SIZES_MIB),
        gap_between_files_s=args.gap_between_files_s,
        gap_between_series_s=args.gap_between_series_s,
        timeout_s=args.timeout_s,
        lock_path=str(args.lock_path),
        rclone=args.rclone,
        rclone_version_stdout=(rclone_version.stdout or "")[:500],
        rclone_extra_flags=extra_flags,
    )
    for series in range(1, args.series_count + 1):
        samples = run_series(
            series=series,
            local_dir=args.local_dir,
            remote=args.remote,
            remote_prefix=args.remote_prefix,
            rclone=args.rclone,
            key_path=args.key_path,
            lock_path=args.lock_path,
            log_path=args.log_path,
            timeout_s=args.timeout_s,
            gap_between_files_s=args.gap_between_files_s,
            include_download=False,
            extra_flags=extra_flags,
        )
        # One reverse download per series on a mid-size successful upload.
        download_candidates = [
            s
            for s in samples
            if s["direction"] == "upload"
            and s["success"]
            and s["size_mib"] == args.download_size_mib
        ]
        if download_candidates:
            chosen = download_candidates[0]
            samples.append(
                _transfer_one(
                    log_path=args.log_path,
                    series=series,
                    direction="download",
                    size_mib=int(chosen["size_mib"]),
                    local_path=Path(str(chosen["local"])),
                    remote=args.remote,
                    remote_name=f"{args.remote_prefix}/bench_s{series}_{chosen['size_mib']}m.bin",
                    rclone=args.rclone,
                    key_path=args.key_path,
                    lock_path=args.lock_path,
                    timeout_s=args.timeout_s,
                    extra_flags=extra_flags,
                )
            )
        all_samples.extend(samples)
        if series < args.series_count:
            _emit(
                args.log_path,
                "series_sleep",
                series=series,
                sleep_s=args.gap_between_series_s,
            )
            time.sleep(args.gap_between_series_s)

    summary = summarize(all_samples)
    summary["rclone"] = args.rclone
    summary["rclone_extra_flags"] = extra_flags
    summary["rclone_version_stdout"] = (rclone_version.stdout or "")[:500]
    summary_path = args.log_path.with_suffix(".summary.json")
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    _emit(args.log_path, "benchmark_complete", summary_path=str(summary_path), **summary)
    print(json.dumps(summary, indent=2, sort_keys=True), flush=True)
    return 0 if summary["upload_failed"] == 0 and summary["download_failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
