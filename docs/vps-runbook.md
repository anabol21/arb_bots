# VPS Runbook

## Purpose
This runbook defines how to reason about the live runtime of `app/screaner_b_o.py` on the VPS.

The repository has three distinct environments:

1. Local development machine
2. VPS runtime host
3. Remote storage server mounted into the VPS filesystem

Do not confuse them.

## Current production host
- SSH: `root@38.180.94.108`
- Migrated **2026-08-10** from `root@38.244.198.42` (historical evidence docs may still cite the old IP).

## Main Runtime Entrypoint
The main runtime script is:

```bash
python3 app/screaner_b_o.py
```

## Single-instance operational requirement
The runtime and its in-process recovery worker have no inter-process lock.
Deployment and systemd procedures must guarantee a single running instance.

**Never start a second instance before the first instance has stopped.**

Check before every manual start:

```bash
pgrep -af 'app/screaner_b_o.py'
```

If this command prints an existing runtime PID, do not start another process.
Stop the existing managed service/process, confirm that `pgrep` returns no
runtime, and only then start the replacement. Two concurrent instances can
duplicate market collection and race while recovering or deleting the same
spool file.

## Current storage design (Design A + durable local spool)
The normal path writes directly to mounted storage. If the mount fails,
already accepted publisher jobs are written to a crash-safe local spool.

Absolute paths (no cwd dependence):

| Role | Path |
|------|------|
| Parquet root | `/mnt/storage/spreads_parquet_by_coins` |
| Same-FS temp | `/mnt/storage/spreads_parquet_by_coins/.tmp` |
| Local durable spool | `/root/spool/<base_coin>/<event_date>` |
| Runtime log | `/root/runtime.log` |
| Failed batch log | `/root/failed_batches.log` |

Hive layout:

```text
/mnt/storage/spreads_parquet_by_coins/
  base_coin=<COIN>/
    event_date=<YYYY-MM-DD>/
      batch_<utc_ms>_<pid>_<seq>_<uuid>.parquet
```

Optional overrides (must remain absolute):

- `SPREAD_PARQUET_ROOT`
- `SPREAD_RUNTIME_LOG`
- `SPREAD_FAILED_BATCHES_LOG`

## Local spool and recovery
Spool files use the same parquet payload as the remote partition and retain
their original `batch_id`:

```text
/root/spool/
  <COIN>/
    <YYYY-MM-DD>/
      batch_<utc_ms>_<pid>_<seq>_<uuid>.parquet
```

Spool publication is `tmp write -> fsync -> parquet read-back -> atomic rename
-> directory fsync`. A spool file is therefore eligible for recovery only
after its final `.parquet` name exists.

The recovery worker runs in the runtime process every 30 seconds by default.
It verifies `/mnt/storage`, copies each spool file through the mounted `.tmp`
directory, validates rows and schema, atomically publishes the original
`batch_id`, and only then deletes the local spool file. An already existing,
valid remote file with the same `batch_id` is treated as an idempotent
recovery. Successful cleanup emits `spool_recovered`.

Configuration:

- `SPREAD_SPOOL_MAX_BYTES` — maximum final spool bytes; default 20 GiB.
- `SPREAD_SPOOL_MAX_FILES` — maximum final spool files; default 100000.
- `SPREAD_SPOOL_TTL_HOURS` — stale-file alert threshold; default 6 hours.
- `SPREAD_SPOOL_RECOVERY_INTERVAL_SEC` — recovery interval; default 30 seconds.

Heartbeat reports `spool_files_count`, `spool_bytes_total`,
`spool_recovered_total`, and `spool_recovery_failed_total`.

### `spool_quota_exceeded`
This is a critical fail-fast condition. The runtime stops accepting new
batches rather than filling the VPS disk.

Manual response:

1. Keep the runtime stopped and do not delete spool parquet files.
2. Check free local disk space and inspect `/root/spool` file count and size.
3. Restore and validate `/mnt/storage`.
4. Restart the runtime so recovery can publish the backlog.
5. Confirm `spool_recovered` events and matching remote parquet files.
6. Increase a quota only after confirming that the VPS has sufficient local
   capacity; never use a quota increase as a substitute for mount recovery.

### `spool_stale_alert`
TTL is monitoring-only: stale files are never deleted automatically.

Manual response:

1. Inspect the named spool file and its entry in `/root/failed_batches.log`.
2. Validate mount health and inspect `spool_recovery_failed` errors.
3. Restart the runtime after mount repair if recovery is not running.
4. Confirm `spool_recovered` before considering the incident resolved.

Only confirmed recovery removes spool files. Manual deletion risks permanent
data loss and requires explicit operator approval.

### Hanging mounted-storage syscall
The heartbeat timeout detects an unresponsive mount and initiates fail-fast
shutdown, but it cannot interrupt a writer thread already blocked inside
`pq.write_table`, `fsync`, read-back, rename, or filesystem metadata access.
That in-flight partition cannot enter the local spool until the syscall
returns. A thread-based timeout reports the hang but does not cancel the
underlying kernel I/O.

## What this design does NOT use
Lifecycle directories `ready` / `uploading` / `uploaded` / `failed` are **not** part of the current runtime.
Those were an aspirational staging model (Design B) and must not be assumed present.

## Publish semantics
Background publisher emits explicit log events:

1. `write_started` — batch accepted by writer; tmp path known
2. `published` — parquet read-back OK + atomic rename to final path
3. `failed` — any failure before publish (including enqueue backpressure timeout)

Only `published` means data is durably present as a final parquet file on `/mnt/storage`.

## Current Priority
- parquet persistence reliability
- writer off hot path
- shutdown drain
- structured storage logs
- mounted storage correctness

Ingest and spread logic are frozen unless explicitly unlocked.

## Validation Philosophy
Never conclude "storage works" from local code inspection alone.

A storage fix is only credible if it includes:
- mount health check
- runtime log inspection
- published parquet inspection under `/mnt/storage/spreads_parquet_by_coins`
- success criteria tied to actual files

## Safe Read-Only VPS Commands

### Identify runtime process
```bash
ps -ef | grep screaner_b_o.py | grep -v grep
pgrep -af screaner_b_o.py
```

### Inspect mount health
```bash
mount | grep /mnt/storage
df -h /mnt/storage
python3 validation/check_mount.py
```

### Inspect published parquet tree
```bash
find /mnt/storage/spreads_parquet_by_coins -maxdepth 4 -type d | sort | head -n 200
find /mnt/storage/spreads_parquet_by_coins -name '*.parquet' | sort | tail -n 50
python3 validation/check_published_parquet.py
```

### Inspect logs
```bash
tail -n 100 /root/runtime.log
grep -E "write_started|published|failed|shutdown|heartbeat|publisher_" /root/runtime.log | tail -n 100
```

### Legacy local tree (pre-Design A)
Historical local writes may still exist at `/root/output/spreads_parquet_by_coins`.
Runtime must no longer write there after Slice 1.

## What to Record During Debugging
For every storage incident, record:

- exact command used to run the script
- current git revision or copied script state
- runtime log path (`/root/runtime.log`)
- parquet root (`/mnt/storage/spreads_parquet_by_coins`)
- queue_depth / failures from heartbeat
- last `published` / `failed` lines
- error text, if any

## Minimum Success Criteria
A storage-related fix is acceptable only if:

1. The script keeps running on VPS.
2. The mount is readable/writable at validation time.
3. Final parquet files appear under `/mnt/storage/spreads_parquet_by_coins`.
4. Log events distinguish `write_started` / `published` / `failed`.
5. Restart does not overwrite existing batch files.
6. Shutdown drain is logged (`shutdown_flush_done`).
