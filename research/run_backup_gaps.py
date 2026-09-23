#!/usr/bin/env python3
"""Fill gear22_bt_features gaps from rclone backup after ticks_sept chunk1.

Writes only /data/experiments/gear22_bt_features and /tmp/spread_bt_scratch.
Never touches /data/live, /data/compacted, /data/bbot-*.
"""
from __future__ import annotations

import fcntl
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

BUILD = Path("/data/experiments/gear22_bt_features_build")
OUT = Path("/data/experiments/gear22_bt_features")
TICKS_SEPT = Path("/data/experiments/gear22_ticks_sept")
SCRATCH_ROOT = Path("/tmp/spread_bt_scratch")
COINS_FILE = BUILD / "_floor_canary_coins_html30.txt"
PROBE = BUILD / "research" / "gear22_feature_day_probe.py"
PYTHON = "/root/venv/bin/python"
RCLONE = "/opt/rclone-1.74.4/rclone"
REMOTE = "backup1tb:spread-compacted"
LOCK_PATH = Path("/run/spread-backup.lock")
MANIFEST_PATH = OUT / "MANIFEST_BACKUP_GAPS.json"

BAR_MS = 300_000
FLOOR_WARMUP_MS = 13 * 3_600_000
BATCH = 10
LOAD_PAUSE = 2.0
RSS_JUMP_KB = 250_000
RSS_HARD_KB = 1_200_000
MIN_FREE_BYTES = 8 * 1024**3
FORBIDDEN_PREFIXES = ("/data/live", "/data/compacted", "/data/bbot")


def log(msg: str) -> None:
    print(msg, flush=True)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_iso(s: str) -> datetime:
    raw = s.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def fmt_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fmt_file_ts(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def floor_5m_ms(ms: int) -> int:
    return (int(ms) // BAR_MS) * BAR_MS


def until_now_iso() -> str:
    ms = int(utc_now().timestamp() * 1000)
    ms = floor_5m_ms(ms) - BAR_MS
    return fmt_iso(datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc))


def loadavg1() -> float:
    return float(Path("/proc/loadavg").read_text().split()[0])


def collector_rss_kb() -> int:
    try:
        out = subprocess.check_output(["pgrep", "-f", "app/screaner_b_o.py"], text=True)
    except subprocess.CalledProcessError:
        return 0
    total = 0
    for pid in out.split():
        status = Path(f"/proc/{pid}/status")
        if not status.is_file():
            continue
        for line in status.read_text().splitlines():
            if line.startswith("VmRSS:"):
                total += int(line.split()[1])
                break
    return total


def pause_if_needed(baseline_rss: int) -> None:
    while True:
        la = loadavg1()
        rss = collector_rss_kb()
        jump = rss - baseline_rss if baseline_rss and rss else 0
        if la > LOAD_PAUSE:
            log(f"PAUSE loadavg={la:.2f} > {LOAD_PAUSE} collector_rss_kb={rss}")
            time.sleep(60)
            continue
        if rss and (jump >= RSS_JUMP_KB or rss >= RSS_HARD_KB):
            log(f"PAUSE collector_rss jump kb={jump} rss={rss} baseline={baseline_rss}")
            time.sleep(60)
            continue
        return


def parse_coins() -> list[str]:
    raw = COINS_FILE.read_text(encoding="utf-8")
    coins: list[str] = []
    for part in raw.replace("\n", ",").split(","):
        tok = part.strip().upper()
        if tok:
            coins.append(tok)
    if len(coins) != 30:
        raise SystemExit(f"expected 30 coins, got {len(coins)}")
    return coins


def batches(coins: list[str]) -> list[list[str]]:
    return [coins[i : i + BATCH] for i in range(0, len(coins), BATCH)]


def assert_safe_path(path: Path) -> None:
    resolved = str(path.resolve())
    for prefix in FORBIDDEN_PREFIXES:
        if resolved == prefix or resolved.startswith(prefix + "/"):
            raise SystemExit(f"refusing path {resolved}")


def free_bytes(path: Path) -> int:
    st = os.statvfs(str(path))
    return int(st.f_bavail * st.f_frsize)


def compacted_names(start_ms: int, end_ms: int) -> list[str]:
    t = floor_5m_ms(start_ms) - BAR_MS
    end = int(end_ms) + BAR_MS
    names: list[str] = []
    while t < end:
        names.append(f"spread_{fmt_file_ts(t)}_{fmt_file_ts(t + BAR_MS)}.parquet")
        t += BAR_MS
    return names


def wait_backup_transfer_idle() -> None:
    while True:
        try:
            rc = subprocess.call(
                ["systemctl", "is-active", "--quiet", "spread-backup-transfer.service"]
            )
        except OSError:
            rc = 1
        if rc != 0:
            return
        log("wait spread-backup-transfer.service active")
        time.sleep(15)


def rclone_copy(names: list[str], dest: Path) -> int:
    dest.mkdir(parents=True, exist_ok=True)
    assert_safe_path(dest)
    if not names:
        return 0
    list_path = dest / "_rclone_files.txt"
    list_path.write_text("\n".join(names) + "\n", encoding="utf-8")
    wait_backup_transfer_idle()
    lock_path = LOCK_PATH
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    cmd = [
        RCLONE,
        "copy",
        REMOTE,
        str(dest),
        "--files-from",
        str(list_path),
        "--no-traverse",
        "--bwlimit",
        "8M",
        "--transfers",
        "1",
        "--checkers",
        "4",
        "--retries",
        "3",
        "--low-level-retries",
        "5",
    ]
    try:
        log(f"lock wait {lock_path} rclone files={len(names)}")
        fcntl.flock(fd, fcntl.LOCK_EX)
        wait_backup_transfer_idle()
        log("rclone start " + " ".join(cmd[1:8]))
        rc = subprocess.call(cmd)
        log(f"rclone done rc={rc}")
        return rc
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


def prepare_scratch(since: str, until: str) -> Path:
    t0 = int(parse_iso(since).timestamp() * 1000)
    t1 = int(parse_iso(until).timestamp() * 1000)
    read0 = t0 - FLOOR_WARMUP_MS
    names = compacted_names(read0, t1)
    shard = f"{parse_iso(since).strftime('%Y%m%dT%H%M%S')}_{parse_iso(until).strftime('%Y%m%dT%H%M%S')}"
    scratch = SCRATCH_ROOT / shard
    if scratch.exists():
        shutil.rmtree(scratch)
    scratch.mkdir(parents=True, exist_ok=True)
    assert_safe_path(scratch)
    local = 0
    need: list[str] = []
    for name in names:
        src = TICKS_SEPT / name
        dst = scratch / name
        if src.is_file():
            os.symlink(src, dst)
            local += 1
        else:
            need.append(name)
    log(f"scratch {scratch} names={len(names)} symlink_ticks_sept={local} rclone_need={len(need)}")
    free = free_bytes(Path("/"))
    log(f"disk free_bytes={free} min={MIN_FREE_BYTES}")
    if free < MIN_FREE_BYTES:
        raise SystemExit(f"disk too tight free={free}")
    if need:
        rc = rclone_copy(need, scratch)
        if rc != 0:
            have = list(scratch.glob("spread_*.parquet"))
            log(f"WARN rclone rc={rc} scratch_files={len(have)}")
            if len(have) < max(12, local):
                raise SystemExit(f"rclone failed rc={rc} and scratch too empty")
    have_n = len(list(scratch.glob("spread_*.parquet")))
    log(f"scratch ready files={have_n}")
    return scratch


def cleanup_scratch(scratch: Path) -> None:
    assert_safe_path(scratch)
    if scratch.exists() and scratch.is_dir() and str(scratch).startswith(str(SCRATCH_ROOT)):
        shutil.rmtree(scratch)
        log(f"scratch deleted {scratch}")


def partition_ok(coin: str, event_date: str, part_name: str) -> bool:
    p = OUT / f"coin={coin}" / f"event_date={event_date}" / part_name
    return p.is_file() and p.stat().st_size > 0


def run_probe(
    since: str,
    until: str,
    coins: list[str],
    data_root: Path,
    part_name: str,
) -> int:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(BUILD)
    env["PYTHONUNBUFFERED"] = "1"
    cmd = [
        PYTHON,
        str(PROBE),
        "--data-root",
        str(data_root),
        "--coins",
        ",".join(coins),
        "--since",
        since,
        "--until",
        until,
        "--out-dir",
        str(OUT),
        "--workers",
        "1",
        "--part-name",
        part_name,
        "--no-manifest",
        "--no-status-ok",
        "--skip-done",
        "--sanity-date",
        "",
    ]
    log("RUN " + " ".join(cmd[4:]))
    return subprocess.call(cmd, cwd=str(BUILD), env=env)


def planned_shards() -> list[dict[str, str]]:
    until_now = until_now_iso()
    shards = [
        {
            "since": "2026-08-27T12:00:00Z",
            "until": "2026-08-28T00:00:00Z",
            "event_date": "2026-08-27",
            "part_name": "part-001.parquet",
            "note": "afternoon 08-27; do not overwrite Mac part-000 00:00-12:00",
        },
        {
            "since": "2026-08-28T00:00:00Z",
            "until": "2026-08-29T00:00:00Z",
            "event_date": "2026-08-28",
            "part_name": "part-000.parquet",
            "note": "backup full day",
        },
        {
            "since": "2026-08-29T00:00:00Z",
            "until": "2026-08-30T00:00:00Z",
            "event_date": "2026-08-29",
            "part_name": "part-000.parquet",
            "note": "backup full day",
        },
        {
            "since": "2026-08-30T00:00:00Z",
            "until": "2026-08-31T00:00:00Z",
            "event_date": "2026-08-30",
            "part_name": "part-000.parquet",
            "note": "backup full day",
        },
        {
            "since": "2026-08-31T00:00:00Z",
            "until": "2026-09-01T00:00:00Z",
            "event_date": "2026-08-31",
            "part_name": "part-000.parquet",
            "note": "backup full day",
        },
        {
            "since": "2026-09-07T21:35:00Z",
            "until": "2026-09-08T00:00:00Z",
            "event_date": "2026-09-07",
            "part_name": "part-001.parquet",
            "note": "after ticks_sept; warmup from ticks_sept + backup tail; do not overwrite chunk1 part-000",
        },
    ]
    day = datetime(2026, 9, 8, tzinfo=timezone.utc)
    end = parse_iso(until_now)
    while day < end:
        nxt = day + timedelta(days=1)
        until = min(nxt, end)
        if until <= day:
            break
        shards.append(
            {
                "since": fmt_iso(day),
                "until": fmt_iso(until),
                "event_date": day.strftime("%Y-%m-%d"),
                "part_name": "part-000.parquet",
                "note": "backup after ticks_sept; warmup ticks_sept and/or prior backup scratch",
            }
        )
        day = nxt
    return shards


def write_manifest(shards: list[dict[str, str]], coins: list[str], status: str) -> None:
    obj = {
        "schema_version": "gear22_bt_features_v1",
        "status": status,
        "coins": coins,
        "n_coins": len(coins),
        "source": "backup1tb:spread-compacted plus ticks_sept warmup when present",
        "scratch": str(SCRATCH_ROOT),
        "disk_plan": "per-shard scratch: symlink ticks_sept hits, rclone missing 5m files with bwlimit 8M, delete scratch after features",
        "part_split": {
            "2026-08-27": {
                "part-000.parquet": "00:00-12:00 Mac; do not overwrite",
                "part-001.parquet": "12:00-24:00 this backup job",
            },
            "2026-09-07": {
                "part-000.parquet": "00:00-21:35 chunk1 ticks_sept; do not overwrite",
                "part-001.parquet": "21:35-24:00 this backup job",
            },
        },
        "shards": shards,
        "updated": fmt_iso(utc_now()),
    }
    tmp = MANIFEST_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(MANIFEST_PATH)


def main() -> int:
    os.chdir(BUILD)
    OUT.mkdir(parents=True, exist_ok=True)
    SCRATCH_ROOT.mkdir(parents=True, exist_ok=True)
    assert_safe_path(OUT)
    assert_safe_path(SCRATCH_ROOT)
    coins = parse_coins()
    groups = batches(coins)
    shards = planned_shards()
    baseline = collector_rss_kb()
    log(
        f"backup-gaps start shards={len(shards)} "
        f"until_now={until_now_iso()} load={loadavg1():.2f} "
        f"collector_rss_kb={baseline} free_g={free_bytes(Path('/')) / 1024**3:.1f}"
    )
    write_manifest(shards, coins, "running")
    for spec in shards:
        since, until = spec["since"], spec["until"]
        event_date, part_name = spec["event_date"], spec["part_name"]
        pending = [c for c in coins if not partition_ok(c, event_date, part_name)]
        if not pending:
            log(f"skip-done {event_date} {part_name} {since} → {until}")
            continue
        log(
            f"shard {since} → {until} date={event_date} part={part_name} "
            f"pending={len(pending)} note={spec.get('note')}"
        )
        pause_if_needed(baseline)
        scratch = prepare_scratch(since, until)
        try:
            for group in groups:
                need = [c for c in group if not partition_ok(c, event_date, part_name)]
                if not need:
                    log(f"skip-done {event_date} {part_name} coins={','.join(group)}")
                    continue
                pause_if_needed(baseline)
                rc = run_probe(since, until, need, scratch, part_name)
                if rc != 0:
                    log(f"FAIL probe rc={rc} {since} {until} coins={need}")
                    write_manifest(shards, coins, "failed")
                    return rc
                log(
                    f"done {event_date} {part_name} n={len(need)} "
                    f"load={loadavg1():.2f} collector_rss_kb={collector_rss_kb()}"
                )
        finally:
            cleanup_scratch(scratch)
        still = [c for c in coins if not partition_ok(c, event_date, part_name)]
        if still:
            log(f"FAIL missing parts {event_date} {part_name} {still[:8]}")
            write_manifest(shards, coins, "failed")
            return 2
        log(f"complete {event_date} {part_name}")
    write_manifest(shards, coins, "complete")
    log("backup-gaps complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
