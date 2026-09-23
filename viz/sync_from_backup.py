#!/usr/bin/env python3
"""Incremental sync of compacted lean ticks from backup → Mac output/lean_ticks.

SoT: backup1tb:spread-compacted. Local dir is a cache for viz / model.

Prefer Mac rclone if remote ``backup1tb`` exists. Else low-nice VPS hop:
  nice -n 19 ionice -c3 rclone → /root/mac_lean_pull → rsync to Mac.

Never reads /data/live. Not a live query path for the UI.
Do not run as a systemd service on the collector VPS.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from research.lean_ticks_io import parse_lean_file_window
from viz.config import (
    DEFAULT_BACKUP_REMOTE,
    DEFAULT_RCLONE_BIN,
    DEFAULT_TICKS,
    DEFAULT_TRANSFERS,
    DEFAULT_VPS_HOST,
    DEFAULT_VPS_STAGING,
)


def _run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    print("+", " ".join(cmd), flush=True)
    return subprocess.run(cmd, check=check)


def mac_rclone_available(remote: str) -> bool:
    if not shutil.which("rclone"):
        return False
    r = subprocess.run(
        ["rclone", "listremotes"],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        return False
    name = remote.split(":", 1)[0] + ":"
    return name in r.stdout.splitlines() or any(
        line.strip() == name for line in r.stdout.splitlines()
    )


def list_remote_names_mac(remote: str) -> set[str]:
    r = subprocess.run(
        ["rclone", "lsf", remote, "--include", "spread_*.parquet"],
        capture_output=True,
        text=True,
        check=True,
    )
    return {ln.strip().rstrip("/") for ln in r.stdout.splitlines() if ln.strip()}


def list_remote_names_vps(
    vps_host: str,
    remote: str,
    rclone_bin: str,
) -> set[str]:
    remote_cmd = (
        f"{rclone_bin} lsf {remote} --include 'spread_*.parquet'"
    )
    r = subprocess.run(
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=20",
            vps_host,
            remote_cmd,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return {ln.strip().rstrip("/") for ln in r.stdout.splitlines() if ln.strip()}


def local_names(ticks: Path) -> set[str]:
    if not ticks.is_dir():
        return set()
    return {p.name for p in ticks.glob("spread_*.parquet")}


def sync_mac_rclone(
    *,
    remote: str,
    dest: Path,
    missing: list[str],
    transfers: int,
) -> int:
    if not missing:
        return 0
    dest.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", suffix=".files", delete=False) as fh:
        for name in missing:
            fh.write(name + "\n")
        list_path = fh.name
    try:
        _run(
            [
                "rclone",
                "copy",
                remote,
                str(dest),
                "--files-from",
                list_path,
                "--transfers",
                str(transfers),
                "--checkers",
                str(max(transfers, 4)),
                "--progress",
            ]
        )
    finally:
        Path(list_path).unlink(missing_ok=True)
    return len(missing)


def sync_vps_hop(
    *,
    vps_host: str,
    remote: str,
    rclone_bin: str,
    staging: str,
    dest: Path,
    missing: list[str],
    transfers: int,
) -> int:
    if not missing:
        return 0
    dest.mkdir(parents=True, exist_ok=True)
    # Upload file list
    with tempfile.NamedTemporaryFile("w", suffix=".files", delete=False) as fh:
        for name in missing:
            fh.write(name + "\n")
        local_list = Path(fh.name)

    remote_list = f"/tmp/viz_lean_files_{Path(local_list).name}"
    try:
        _run(["scp", "-o", "BatchMode=yes", str(local_list), f"{vps_host}:{remote_list}"])
        # Low priority rclone into staging (not /data/live)
        remote_sh = (
            f"mkdir -p {staging} && "
            f"nice -n 19 ionice -c3 {rclone_bin} copy {remote} {staging} "
            f"--files-from {remote_list} --transfers {transfers} --checkers {transfers} "
            f"&& rm -f {remote_list}"
        )
        _run(
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                vps_host,
                remote_sh,
            ]
        )
        # rsync only the missing names
        with tempfile.NamedTemporaryFile("w", suffix=".rsync", delete=False) as fh:
            for name in missing:
                fh.write(name + "\n")
            rsync_list = fh.name
        try:
            _run(
                [
                    "rsync",
                    "-avP",
                    "--files-from",
                    rsync_list,
                    f"{vps_host}:{staging}/",
                    str(dest) + "/",
                ]
            )
        finally:
            Path(rsync_list).unlink(missing_ok=True)
        # Best-effort cleanup of staged copies (not /data/*)
        quoted = " ".join(f"'{n}'" for n in missing)
        subprocess.run(
            ["ssh", "-o", "BatchMode=yes", vps_host, f"cd {staging} && rm -f -- {quoted}"],
            check=False,
        )
    finally:
        local_list.unlink(missing_ok=True)
    return len(missing)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--ticks", type=Path, default=DEFAULT_TICKS)
    p.add_argument("--remote", default=DEFAULT_BACKUP_REMOTE)
    p.add_argument("--vps-host", default=DEFAULT_VPS_HOST)
    p.add_argument("--vps-staging", default=DEFAULT_VPS_STAGING)
    p.add_argument("--rclone-bin", default=DEFAULT_RCLONE_BIN)
    p.add_argument("--transfers", type=int, default=DEFAULT_TRANSFERS)
    p.add_argument(
        "--force-vps-hop",
        action="store_true",
        help="use VPS rclone hop even if Mac rclone remote exists",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="only print missing count / sample",
    )
    p.add_argument(
        "--rebuild-catalog",
        action="store_true",
        help="rebuild viz DuckDB catalog after sync",
    )
    p.add_argument(
        "--refresh-overview",
        action="store_true",
        help="incremental overview update (new files only; not a full rescan)",
    )
    args = p.parse_args(argv)

    dest = args.ticks.resolve()
    dest.mkdir(parents=True, exist_ok=True)

    use_mac = (not args.force_vps_hop) and mac_rclone_available(args.remote)
    print(
        f"mode={'mac-rclone' if use_mac else 'vps-hop'} remote={args.remote}",
        flush=True,
    )

    if use_mac:
        remote_names = list_remote_names_mac(args.remote)
    else:
        remote_names = list_remote_names_vps(
            args.vps_host, args.remote, args.rclone_bin
        )

    local = local_names(dest)
    missing = sorted(remote_names - local)
    # Prefer valid window names only
    missing = [n for n in missing if parse_lean_file_window(Path(n)) is not None]

    print(
        f"remote={len(remote_names)} local={len(local)} missing={len(missing)}",
        flush=True,
    )
    if missing[:5]:
        print("sample missing:", ", ".join(missing[:5]), flush=True)

    if args.dry_run:
        return 0

    if not missing:
        print("nothing to sync", flush=True)
    elif use_mac:
        sync_mac_rclone(
            remote=args.remote,
            dest=dest,
            missing=missing,
            transfers=args.transfers,
        )
    else:
        sync_vps_hop(
            vps_host=args.vps_host,
            remote=args.remote,
            rclone_bin=args.rclone_bin,
            staging=args.vps_staging,
            dest=dest,
            missing=missing,
            transfers=args.transfers,
        )

    if args.rebuild_catalog:
        from viz.catalog import rebuild_catalog
        from viz.config import DEFAULT_CATALOG

        info = rebuild_catalog(dest, DEFAULT_CATALOG)
        print(
            f"catalog rebuilt n_files={info['n_files']} "
            f"{info['start']} → {info['end']}",
            flush=True,
        )

    if args.refresh_overview:
        from viz.overview_cache import build_incremental

        ov = build_incremental(dest, workers=args.transfers)
        print(f"overview incremental: {ov}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
