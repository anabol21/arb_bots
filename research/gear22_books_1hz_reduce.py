"""Reduce backup compacted ticks to a 1 Hz last-book table. Run on the VPS.

Each batch copies a few 5-minute windows from ``backup1tb:spread-compacted``
into ``/tmp``, keeps the last tick of each UTC second for the canary coins,
writes one parquet part, then deletes those raw files. The backup remote is
never deleted. ``/data/live`` and ``/data/compacted`` are not used.

On the 6-vCPU / 15 GiB VPS, run it under a transient unit so the job can
use half the CPUs and no more than 8 GiB, leaving the other half for the
collector and the bbot units: ``CPUQuota=300%``, ``MemoryMax=8G``,
``MemorySwapMax=0``. The script does not apply those limits itself.

The kept tick is the last row of clock-second ``event_local_ts_ms // 1000``.
A later lookup "last tick with ts <= T" walks backward across missing seconds.
This is the book attachment for the gear-2.2 cash ledger, not a new feature
schema and not a second copy of the raw ticks.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

REMOTE = "backup1tb:spread-compacted"
RCLONE = "/opt/rclone-1.74.4/rclone"
LOCK_PATH = Path("/run/spread-backup.lock")
SCRATCH = Path("/tmp/spread_books_1hz_scratch")
DEFAULT_OUT = Path("/data/experiments/gear22_books_1hz")
FORBIDDEN_PREFIXES = ("/data/live", "/data/compacted", "/data/bbot")
MIN_FREE_BYTES = 8 * 1024**3
BATCH = 12  # one hour of 5-minute windows
SINCE_FILE_TS = "20260827T114000Z"
READ_COLS = (
    "event_local_ts_ms",
    "base_coin",
    "bybit_bid_price",
    "bybit_ask_price",
    "okx_bid_price",
    "okx_ask_price",
)


def log(msg: str) -> None:
    print(msg, flush=True)


def assert_safe(path: Path) -> None:
    resolved = str(path.resolve())
    for prefix in FORBIDDEN_PREFIXES:
        if resolved == prefix or resolved.startswith(prefix + "/"):
            raise SystemExit(f"refusing path {resolved}")


def free_bytes(path: Path) -> int:
    st = os.statvfs(str(path))
    return int(st.f_bavail * st.f_frsize)


def load_coins(path: Path) -> list[str]:
    coins: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        for part in line.replace(",", " ").split():
            tok = part.strip().upper()
            if tok and not tok.startswith("#"):
                coins.append(tok)
    if not coins:
        raise SystemExit(f"no coins in {path}")
    return coins


def file_start_ts(name: str) -> str:
    # spread_YYYYMMDDTHHMMSSZ_YYYYMMDDTHHMMSSZ.parquet
    stem = name[len("spread_") : -len(".parquet")]
    return stem.split("_", 1)[0]


def last_tick_per_second(table: pa.Table, coins: set[str]) -> pa.Table:
    """Keep the last row of each (coin, UTC second)."""
    if table.num_rows == 0:
        return _empty()
    coin_col = table.column("base_coin")
    if pa.types.is_dictionary(coin_col.type):
        coin_col = pc.dictionary_decode(coin_col)
    names = [str(x).upper() if x is not None else "" for x in coin_col.to_pylist()]
    ts = table.column("event_local_ts_ms").to_numpy(zero_copy_only=False).astype(np.int64, copy=False)
    keep = np.fromiter((n in coins for n in names), dtype=bool, count=len(names))
    if not keep.any():
        return _empty()
    idx = np.flatnonzero(keep)
    coin_k = np.array([names[i] for i in idx])
    # Primary key coin, secondary key time, so the last row of a second is last.
    order_local = np.lexsort((ts[idx], coin_k))
    chosen_idx = idx[order_local]
    sec = ts[chosen_idx] // 1000
    coin_s = coin_k[order_local]
    last = np.ones(chosen_idx.shape[0], dtype=bool)
    if chosen_idx.shape[0] > 1:
        same = (coin_s[:-1] == coin_s[1:]) & (sec[:-1] == sec[1:])
        last[:-1] = ~same
    chosen = chosen_idx[last]
    bid_b = table.column("bybit_bid_price").to_numpy(zero_copy_only=False)[chosen]
    ask_b = table.column("bybit_ask_price").to_numpy(zero_copy_only=False)[chosen]
    bid_o = table.column("okx_bid_price").to_numpy(zero_copy_only=False)[chosen]
    ask_o = table.column("okx_ask_price").to_numpy(zero_copy_only=False)[chosen]
    ts_c = ts[chosen]
    return pa.table(
        {
            "ts_s": (ts_c // 1000).astype(np.int64),
            "base_coin": pa.array([names[i] for i in chosen], type=pa.string()),
            "event_local_ts_ms": ts_c.astype(np.int64),
            "bybit_bid_price": bid_b.astype(np.float64),
            "bybit_ask_price": ask_b.astype(np.float64),
            "okx_bid_price": bid_o.astype(np.float64),
            "okx_ask_price": ask_o.astype(np.float64),
        }
    )


def _empty() -> pa.Table:
    return pa.table(
        {
            "ts_s": pa.array([], type=pa.int64()),
            "base_coin": pa.array([], type=pa.string()),
            "event_local_ts_ms": pa.array([], type=pa.int64()),
            "bybit_bid_price": pa.array([], type=pa.float64()),
            "bybit_ask_price": pa.array([], type=pa.float64()),
            "okx_bid_price": pa.array([], type=pa.float64()),
            "okx_ask_price": pa.array([], type=pa.float64()),
        }
    )


def reduce_files(paths: list[Path], coins: set[str]) -> pa.Table:
    parts: list[pa.Table] = []
    for path in paths:
        table = pq.read_table(path, columns=list(READ_COLS))
        reduced = last_tick_per_second(table, coins)
        if reduced.num_rows:
            parts.append(reduced)
    if not parts:
        return _empty()
    return pa.concat_tables(parts)


def wait_backup_idle() -> None:
    while True:
        try:
            rc = subprocess.call(
                ["systemctl", "is-active", "--quiet", "spread-backup-transfer.service"]
            )
        except OSError:
            return
        if rc != 0:
            return
        log("wait spread-backup-transfer.service")
        time.sleep(20)


def wait_disk() -> None:
    while free_bytes(Path("/")) < MIN_FREE_BYTES:
        log(f"wait disk free={free_bytes(Path('/'))}")
        time.sleep(60)


def rclone_copy(names: list[str], dest: Path) -> int:
    assert_safe(dest)
    dest.mkdir(parents=True, exist_ok=True)
    list_path = dest / "_files.txt"
    list_path.write_text("\n".join(names) + "\n", encoding="utf-8")
    wait_backup_idle()
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(LOCK_PATH), os.O_RDWR | os.O_CREAT, 0o600)
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
        fcntl_lock(fd)
        wait_backup_idle()
        log(f"rclone files={len(names)} first={names[0]} last={names[-1]}")
        return subprocess.call(cmd)
    finally:
        fcntl_unlock(fd)
        os.close(fd)


def fcntl_lock(fd: int) -> None:
    import fcntl

    log(f"lock {LOCK_PATH}")
    fcntl.flock(fd, fcntl.LOCK_EX)


def fcntl_unlock(fd: int) -> None:
    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass


def list_remote() -> list[str]:
    log("rclone lsf start")
    out = subprocess.check_output(
        [RCLONE, "lsf", REMOTE, "--files-only", "--include", "spread_*.parquet"],
        text=True,
    )
    names = [line.strip() for line in out.splitlines() if line.strip().endswith(".parquet")]
    names = [n for n in names if n.startswith("spread_") and file_start_ts(n) >= SINCE_FILE_TS]
    names.sort(key=file_start_ts)
    log(f"remote candidates={len(names)} first={names[0] if names else '-'} last={names[-1] if names else '-'}")
    return names


def load_done(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    return {line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()}


def append_done(path: Path, names: list[str]) -> None:
    with path.open("a", encoding="utf-8") as fh:
        for name in names:
            fh.write(name + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def write_part(out_dir: Path, batch: list[str], table: pa.Table) -> Path:
    stamp = file_start_ts(batch[0])
    final = out_dir / f"part-{stamp}.parquet"
    tmp = out_dir / f".part-{stamp}.parquet.tmp"
    pq.write_table(table, tmp, compression="zstd")
    os.replace(tmp, final)
    check = pq.read_metadata(final)
    if check.num_rows != table.num_rows:
        raise SystemExit(f"row mismatch writing {final}")
    return final


def clear_scratch(scratch: Path) -> None:
    assert_safe(scratch)
    if scratch.exists():
        shutil.rmtree(scratch)
    scratch.mkdir(parents=True, exist_ok=True)


def run_batches(out_dir: Path, coins: list[str], *, batch_size: int) -> None:
    assert_safe(out_dir)
    assert_safe(SCRATCH)
    out_dir.mkdir(parents=True, exist_ok=True)
    done_path = out_dir / "done.txt"
    missing_path = out_dir / "missing.txt"
    done = load_done(done_path)
    coin_set = set(coins)
    names = [n for n in list_remote() if n not in done]
    log(f"todo={len(names)} already_done={len(done)} coins={len(coin_set)} batch={batch_size}")
    passes = 0
    while names and passes < 3:
        passes += 1
        pending = list(names)
        names = []
        for i in range(0, len(pending), batch_size):
            batch = pending[i : i + batch_size]
            wait_disk()
            clear_scratch(SCRATCH)
            rc = rclone_copy(batch, SCRATCH)
            got = sorted(p.name for p in SCRATCH.glob("spread_*.parquet"))
            if not got:
                log(f"batch empty rc={rc} first={batch[0]}")
                with missing_path.open("a", encoding="utf-8") as fh:
                    for name in batch:
                        fh.write(name + "\n")
                continue
            t0 = time.time()
            table = reduce_files([SCRATCH / name for name in got], coin_set)
            part = write_part(out_dir, got, table)
            append_done(done_path, got)
            clear_scratch(SCRATCH)
            missed = [n for n in batch if n not in got]
            if missed:
                names.extend(missed)
                log(f"retry later missing={len(missed)} first={missed[0]}")
            log(
                f"batch ok part={part.name} raw_files={len(got)} rows={table.num_rows} "
                f"sec={time.time() - t0:.1f} rc={rc}"
            )
        if names:
            log(f"retry pass todo={len(names)}")
            time.sleep(30)
    if names:
        log(f"stopped with still missing={len(names)} first={names[0]}")
    else:
        log("reduce complete")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--coins", type=Path, required=True)
    parser.add_argument("--batch", type=int, default=BATCH)
    args = parser.parse_args()
    if args.batch < 1:
        raise SystemExit("--batch must be >= 1")
    run_batches(args.out, load_coins(args.coins), batch_size=args.batch)


if __name__ == "__main__":
    main()
