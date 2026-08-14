# Bars production bridge validation — 2026-08-12

## Scope and boundary

Track 1, backup-transfer stage on production VPS `root@38.180.94.108`.
The collector remains `spread-collector.service`; first materialization is
`/data/bars/bar_5m/...`; durable destination is
`backup1tb:spread-bars/...`. WebSocket ingest, parsing, spread calculation,
trading, and bar schema are unchanged.

## Read-only lifecycle trace (13:38–13:39 UTC)

Source identity:

```text
bar_5m/base_coin=0G/event_date=2026-08-12/
batch_1786500902142_24505_072949_9d8b50b9.parquet
```

Evidence:

- `/data/bars/sent/<identity>` exists at 3,579 bytes.
- `/data/bars/.state/backup_manifest.sqlite3` records `state=sent`,
  `attempts=1`, `confirmed_at=sent_at=2026-08-12 13:35:28 UTC`, and remote
  `backup1tb:spread-bars/<identity>`.
- Structured transfer log records the required sequence: `copyto` to
  `<identity>.inprogress`, temporary size verification, `moveto` to final,
  final size verification, size-only SHA exception for this 3,579-byte
  microfile, then successful `transfer_result`.
- Exact remote confirmation:

```text
rclone lsl backup1tb:spread-bars/<identity>
3579 2026-08-12 02:15:02.000000000 batch_1786500902142_24505_072949_9d8b50b9.parquet
```

The durable remote path and per-file lifecycle are therefore evidenced for this
sample. This does not prove aggregate throughput or overall durability.

## Baseline bottleneck

The same log records approximately 22.7 seconds per 3.5 KiB bar: each source
starts independent `rclone` commands for final stat, upload, temporary stat,
rename, and final stat. The observed rate is about 1,370 files/hour versus
about 4,044 files/hour input, so the active bars queue grows.

## Candidate bridge and acceptance

`BACKUP_HIVE_BATCH_SIZE=32` makes one local Unix-socket `rclone rcd` session
per bounded lock hold. Each file retains its own atomic
temporary-to-final publishing, final-size check, manifest confirmation, and
local `sent` move. The daemon has been read-only probed against the exact
object with `operations/stat`, returning `item.Size=3579`.

Focused proof remains required before readiness:

- bars drain rate above 4,100 files/hour, or a clear decline in backlog and
  oldest age;
- exact remote object and fresh BTC/ETH/SOL partitions;
- compaction lag at most 30 minutes; OOM=TERM=0; collector `NRestarts=0`;
- tick transfer/lifecycle still healthy.

Only a passing focused proof authorizes the 8–12 hour systemd sampler.

## Deployment stop gate

The first live `32`-file run did not satisfy the compactor-priority boundary:
after the RC missing-object response was corrected (`{"item": null}` is the
normal absent-final response), a micro-batch remained active beyond the
compactor's 90-second wait. It was stopped and the bars timer was disabled to
prevent a repeat while the upper bound is redesigned. The collector remained
active with `NRestarts=0`; this is a strict **NO-GO** for the focused proof and
overnight canary, not a throughput result.

## Approved concurrency experiment (pending staged proof)

The blocker was serialization, not a demonstrated memory limit: the shared
`/run/spread-heavy-storage.lock` was held for more than the compactor's
90-second wait while one persistent-RCD micro-batch was active. Reducing the
batch below 32 would reduce the lock hold but is not accepted as a throughput
solution because it would increase session setup overhead.

On this 16 GiB VPS, the approved experiment removes
`BACKUP_SHARED_LOCK_PATH` only from `spread-bars-backup-transfer.service`.
The bars service retains its exclusive
`BACKUP_TRANSFER_LOCK_PATH=/run/spread-bars-backup.lock`, `MemoryMax=1500M`,
and `BACKUP_HIVE_BATCH_SIZE=32`; each bar retains its atomic
temporary-remote → final-remote → exact-size-confirmed → manifest-confirmed →
local-`sent` lifecycle. The compactor retains
`/run/spread-heavy-storage.lock`, its own lock, and its priority for other
heavy tasks that opt into that shared lock. There is deliberately no mutual
`Conflicts=`.

This changes scheduling, not the durability boundary. It must be assessed with
an active collector and compactor:

1. Establish a baseline for bars backlog count/oldest age, free disk,
   compaction lag, newest BTC/ETH/SOL partitions, collector `NRestarts`, and
   OOM/TERM journal counts.
2. Run a 15-minute guarded smoke. Stop bars immediately for any OOM, TERM,
   remote error, RCD/socket leak, rising compaction lag above 30 minutes, or
   unhealthy collector/compactor.
3. Only then run a 90-minute focused proof. Pass requires over 4,100
   files/hour drain or visibly declining backlog/oldest age; continuous
   completed compaction at lag at most 30 minutes; collector `NRestarts=0`;
   OOM=TERM=0; remote exact-object and BTC/ETH/SOL freshness advancement; and
   healthy ticks/lifecycle/disk.
4. Only a pass starts an 8–12 hour canary. Record baseline, start time, and
   acceptance metrics; do not wait for its completion in this change.

`validation/ops_alerts.py` exposes bars-service activity and compactor-timer
activity alongside its existing compaction completion age, lag, backlog, OOM,
TERM, and micro-batch signals, so concurrent operation is observable rather
than implied by lock configuration.

## Concurrency smoke result — NO-GO (14:01:31–14:18:50 UTC)

The unlocked service ran with one persistent RCD and no shared-heavy-lock
contention. It completed 215/215 transfers in 1,030.980 seconds:

- mean per-file lifecycle: **4.791 seconds**;
- measured drain: **750.7 files/hour**;
- required drain: **more than 4,100 files/hour**.

The bars backlog increased from 108,658 to 109,094 files (oldest age increased
from 2,471 to 2,489 minutes). The failure is throughput, not memory or
compactor starvation: collector stayed active with `NRestarts=0`, collector
and compactor OOM/TERM signals were zero in the experiment window, compaction
lag was 4.088 minutes at stop, `/data` retained 61 GiB free, and the bars
service left no `backup_transfer` or `rclone rcd` process after shutdown.

The bars timer and service were stopped and the timer disabled. No 90-minute
proof or 8–12 hour canary was started. The explicit next experiment must reduce
the approximately five RC round trips per tiny object; lock removal alone
cannot meet the ingress rate.
