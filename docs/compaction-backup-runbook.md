# Compaction and Secondary Backup Runbook

## Pipeline

The live writer, compactor, and backup transfer are separate processes:

```text
writer ticks -> /data/live/*.parquet
compactor    -> /data/compacted/spread_*.parquet
backup_transfer (flat) -> backup1tb:spread-compacted

writer bars  -> /data/bars/bar_5m/.../batch_*.parquet
backup_transfer (hive) -> backup1tb:spread-bars
(bars are not compacted; same confirm-before-local-move pattern)
```

The secondary backup must never be mounted with sshfs in the live write path.
`/data` is a VPS-local directory and may reside on the root filesystem; it is
not assumed to be a dedicated volume. Free-space alerting must therefore cover
the filesystem reported by `df /data`.

## Local paths

| Role | Path |
|---|---|
| Live writer final files | `/data/live/` |
| Live writer temporary files | `/data/live/.tmp/*.parquet.tmp` |
| Archived source files | `/data/live/archived/` |
| Consolidated files | `/data/compacted/spread_*.parquet` |
| Compactor state | `/data/compacted/.state/spread_*.json` |
| Compactor temporary files | `/data/compacted/.tmp/*.inprogress` |
| Confirmed local backup copies | `/data/compacted/sent/` |
| Transfer manifest | `/data/compacted/.state/backup_manifest.sqlite3` |
| Live bar hive | `/data/bars/bar_5m/...` |
| Confirmed local bar copies | `/data/bars/sent/...` |
| Bars transfer manifest | `/data/bars/.state/backup_manifest.sqlite3` |

Defaults:

- compaction source-window width: 5 minutes; compactor timer cadence: 2 minutes
  during catch-up;
- source archive retention: 24 hours;
- transfer schedule: 5 minutes;
- sent retention: 12 hours.

## Backup preflight

The VPS must have:

1. rclone **1.74.4+** installed (side-by-side binary recommended; system
   `/usr/bin/rclone` 1.53.3 lacks `--sftp-concurrency` / `--sftp-chunk-size`);
2. a dedicated private key readable only by root;
3. passwordless SFTP access to `b_442546@5.45.77.77`;
4. an rclone SFTP remote configured without sshfs.

The key path is mandatory and explicit. Do not assume
`/root/.ssh/id_ed25519`.

Recommended VPS environment for `backup_transfer`:

```bash
export BACKUP_RCLONE_BINARY=/opt/rclone-1.74.4/rclone
export BACKUP_RCLONE_REMOTE=backup1tb
export BACKUP_RCLONE_PATH=spread-compacted
export BACKUP_SFTP_KEY_PATH=/root/.ssh/id_ed25519_uploader
export BACKUP_RCLONE_SFTP_CONCURRENCY=8
export BACKUP_RCLONE_SFTP_CHUNK_SIZE=128k
```

Notes:

- Defaults match the measured working tune (`concurrency=8`, `chunk=128k`,
  mean ~1.43 MiB/s, p10 ~0.60 MiB/s). Watchdog floor is 0.5 MiB/s.
- Do **not** use support's exact `32` / `512k` — that pair caused EOF on this
  endpoint.
- Tuning flags apply only to upload `copyto` / `moveto`. SHA-256
  `download_verify` deliberately omits concurrency/chunk to avoid the
  size-mismatch failure seen when downloading with those flags.
- Set `BACKUP_RCLONE_SFTP_CONCURRENCY=0` to disable upload tuning.

Example rclone configuration:

```ini
[backup1tb]
type = sftp
host = 5.45.77.77
user = b_442546
port = 22
key_file = /root/.ssh/<confirmed-working-key>
```

Verify before scheduling:

```bash
printf 'pwd\nquit\n' | sftp -b - -oBatchMode=yes \
  -i /root/.ssh/<confirmed-working-key> b_442546@5.45.77.77

/opt/rclone-1.74.4/rclone lsd backup1tb: --timeout 60s --retries 1
```

## One-shot commands

Run compaction:

```bash
/root/venv/bin/python -m app.storage.compactor \
  --live /data/live \
  --compacted /data/compacted \
  --interval 300 \
  --retention-hours 12 \
  --max-windows 2
```

Salvage / MemoryMax-safe oneshot (one thick window per process):

```bash
/usr/bin/time -v /root/venv/bin/python -m app.storage.compactor \
  --live /data/live \
  --compacted /data/compacted \
  --interval 300 \
  --retention-hours 12 \
  --max-windows 1
```

Retention-only (disk reclaim without compacting new windows):

```bash
/root/venv/bin/python -m app.storage.compactor \
  --live /data/live \
  --compacted /data/compacted \
  --retention-only \
  --retention-hours 12
```

Run transfer:

```bash
BACKUP_RCLONE_BINARY=/opt/rclone-1.74.4/rclone \
BACKUP_RCLONE_REMOTE=backup1tb \
BACKUP_RCLONE_PATH=spread-compacted \
BACKUP_SFTP_KEY_PATH=/root/.ssh/id_ed25519_uploader \
BACKUP_RCLONE_SFTP_CONCURRENCY=8 \
BACKUP_RCLONE_SFTP_CHUNK_SIZE=128k \
/root/venv/bin/python -m app.storage.backup_transfer \
  --compacted-dir /data/compacted \
  --sent-retention-hours 12
```

Bars hive transfer (skip SHA download_verify under 1 MiB; size match still required):

```bash
BACKUP_RCLONE_BINARY=/opt/rclone-1.74.4/rclone \
BACKUP_RCLONE_REMOTE=backup1tb \
BACKUP_RCLONE_PATH=spread-bars \
BACKUP_SFTP_KEY_PATH=/root/.ssh/id_ed25519_uploader \
BACKUP_TRANSFER_LOCK_PATH=/run/spread-bars-backup.lock \
BACKUP_HIVE_BATCH_SIZE=32 \
/root/venv/bin/python -m app.storage.backup_transfer \
  --compacted-dir /data/bars \
  --layout hive \
  --remote-path spread-bars \
  --sent-retention-hours 12 \
  --max-files 500 \
  --hive-batch-size 32 \
  --skip-sha-verify-below-bytes 1048576
```

Each command is one-shot and suitable for cron or a systemd timer.
`backup_transfer` also takes a non-blocking exclusive `fcntl` lock on
`/run/spread-backup.lock` (override with `BACKUP_TRANSFER_LOCK_PATH`) so
overlapping invocations exit with `transfer_skipped_lock_held` instead of
running concurrent rclone uploads. `BACKUP_HIVE_BATCH_SIZE=32` starts one
local `rclone rcd` on a private Unix socket and sends
each file's individual RC `stat → copyfile temporary → stat → movefile final →
stat` lifecycle through the same SFTP backend. It is therefore not a mere
`flock` around five fresh rclone subprocesses per file. Every file still has a
separate final size confirmation, manifest transition, and local move.

`BACKUP_SHARED_LOCK_PATH` remains an optional serialization mechanism for
environments that need it. If configured and compaction owns the shared lock,
bars emits
`transfer_deferred_shared_lock_busy` with the deferred batch size, retries
three times at 0.5 s intervals, then emits `hive_microbatch_deferred` and
continues to the next bounded batch. This preserves compactor priority without
aborting all 500 candidates at the first contention event. External `flock`
remains recommended for compactor. Neither lock is distributed.

```cron
*/5 * * * * flock -n /run/spread-compactor.lock /root/venv/bin/python -m app.storage.compactor --live /data/live --compacted /data/compacted >> /var/log/spread/compactor.log 2>&1
2-59/5 * * * * env BACKUP_RCLONE_BINARY=/opt/rclone-1.74.4/rclone BACKUP_RCLONE_REMOTE=backup1tb BACKUP_RCLONE_PATH=spread-compacted BACKUP_SFTP_KEY_PATH=/root/.ssh/id_ed25519_uploader BACKUP_RCLONE_SFTP_CONCURRENCY=8 BACKUP_RCLONE_SFTP_CHUNK_SIZE=128k BACKUP_TRANSFER_LOCK_PATH=/run/spread-backup.lock /root/venv/bin/python -m app.storage.backup_transfer --compacted-dir /data/compacted >> /var/log/spread/backup-transfer.log 2>&1
```

### systemd packaging (preferred)

Unit/timer snippets live under `deploy/systemd/` with absolute production env:

- `SPREAD_PARQUET_ROOT=/data/live`
- `SPREAD_RUNTIME_LOG=/var/log/spread/runtime.log`
- `SPREAD_FAILED_BATCHES_LOG=/var/log/spread/failed_batches.log`
- `SPREAD_SPOOL_ROOT=/data/spool`
- `BACKUP_RCLONE_BINARY=/opt/rclone-1.74.4/rclone`
- `BACKUP_RCLONE_REMOTE=backup1tb`
- `BACKUP_RCLONE_PATH=spread-compacted` (or the active prod prefix)
- `BACKUP_SFTP_KEY_PATH=/root/.ssh/id_ed25519_uploader`
- `BACKUP_RCLONE_SFTP_CONCURRENCY=8`
- `BACKUP_RCLONE_SFTP_CHUNK_SIZE=128k`

Collector is a long-running service. Tick compactor runs every 2 minutes during
catch-up under its own `flock /run/spread-compactor.lock`, one
`--max-windows 1` window per process, and the shared local
`/run/spread-heavy-storage.lock`. That shared lock protects compactor from
other heavy tasks that opt into it. It is intentionally not acquired by the
16 GiB VPS persistent bars uploader: bars and compactor run concurrently as a
measured production experiment. Return to a five-minute cadence only after
measured capacity demonstrates it can maintain the lag SLO; the current
one-window/five-minute policy has no catch-up margin.

Bars transfer remains on a 20-minute timer. Its independent
`BACKUP_TRANSFER_LOCK_PATH=/run/spread-bars-backup.lock` prevents overlapping
bars transfers. The 16 GiB production unit does not set
`BACKUP_SHARED_LOCK_PATH` and has no `Conflicts=` relationship with the
compactor; its persistent local rclone daemon amortizes SFTP setup across each
32-file micro-batch while compaction continues independently. The focused proof
must measure concurrent memory, compaction freshness, transfer progress, and
RCD cleanup; it must not infer safety from available RAM alone.

The bars batch remains `--max-files 500`. This is the existing canary setting
that already transfers tiny hive files with SHA skipped below 1 MiB; no lower
batch limit is inferred without new duration/backlog evidence.

Install notes: `docs/prod-unit-snippets.md`. Cron alternative:
`deploy/cron/spread-maintenance.cron`.

### Compacted-bars timer proof

The v2 compacted-bars timers are independently scheduled from the legacy bars
timer: `spread-bars-compactor.timer` uses
`OnCalendar=*-*-* *:*:0/10`; `spread-bars-compacted-backup-transfer.timer`
uses `OnCalendar=*-*-* *:*:00`. Both require `Persistent=true`. Do not use
`OnUnitInactiveSec` as the only recurring trigger when restarting timers whose
`Type=oneshot` target is already inactive: systemd can report
`NextElapseUSecMonotonic=infinity` instead of arming a new occurrence.

After deployment, run `systemctl daemon-reload`, restart both timers, and
confirm their calendar `NextElapse` is not `n/a` or `infinity`. Observe at
least two actual service starts for each timer before a throughput canary.

The production v2 roots are `/data/bars_compacted_v2/bar_5m` (with `.state/`
and `sent/` below it), `/data/bars_compacted_v2/archive/bar_5m`, and
`backup1tb:spread-bars-compacted-v2`. The source remains
`/data/bars/bar_5m`. Existing v1 `/data/bars_compacted`,
`/data/bars_archive`, and `backup1tb:spread-bars-compacted` are preserved
read-only: v2 has no automatic migration, deletion, overwrite, or bulk move.
Run `validation/check_compacted_bars.py` with its v2 default root; do not point
the v2 acceptance validator at the legacy root.

### Operator alerts

```bash
/root/venv/bin/python validation/ops_alerts.py --once
```

Alerts cover `/data` free space, archive age/count, compacted backlog files/MB,
active bars hive backlog count/size/oldest age, bars service-active state,
compactor-timer-active state, bars shared-lock deferrals when that optional
mode is configured, bounded micro-batch results/deferrals, and an alert when the oldest active bars
file exceeds `--max-bars-backlog-age-minutes` (default 60),
`transfer_watchdog_kills`, recent `compaction_alert` events, **compaction lag**
(wall clock − newest compacted window), missing `compaction_complete` /
`archive_retention_complete` for N timer cycles, live growth with empty tick
backlog, `transfer_enospc_alert`, and separate best-effort OOM and
`status=15/TERM` signals from journalctl / logs. A high compaction lag is an
alert and cannot produce `ops_alert_ok`.

See also: vacation OOM forensics
[`docs/vacation-break-forensics-20260810.md`](vacation-break-forensics-20260810.md)
and fix validation
[`docs/compactor-fix-validation-20260810.md`](compactor-fix-validation-20260810.md).

## Durability boundaries

- Writer temporary files are never compactor inputs.
- Only final `*.parquet` files from completed mtime windows are compacted.
- Source files remain in `/data/live` until a consolidated final passes exact
  row-count validation.
- Compacted files remain local until the remote final's size and downloaded
  SHA-256 content are confirmed.
- Transfer failure or watchdog termination leaves the compacted source in
  place and only increases backlog.
- `sent/` retention starts from manifest `sent_at`, not the file's historical
  compaction mtime.

## Metrics

Compactor JSON events include:

- `compaction_ratio` (`input_bytes / output_bytes`);
- `compression_pct`;
- `compaction_duration_ms`;
- `row_count_match`;
- input/output bytes, rows, and source file count.

Transfer JSON events include:

- `transfer_duration_s`;
- `transfer_success`;
- `transfer_retry_count`;
- `backlog_files_count`;
- `backlog_size_mb`;
- `transfer_watchdog_kills`.

## Validation experiments

Run only on an approved staging VPS.

### 1. One-hour integrity

1. Run the writer for one hour.
2. Stop it gracefully.
3. Wait until the final 5-minute window closes.
4. Run compactor once, then transfer until backlog reaches zero.
5. Compare every complete compactor manifest's `total_rows` with its local
   consolidated parquet row count.
6. Download the remote consolidated files to a separate validation directory
   and compare their parquet row counts with the corresponding local `sent/`
   files.

Pass: all totals and every per-file row count match exactly.

### 2. Backup network isolation

During active transfer, block only traffic to `5.45.77.77` for a short window.
The firewall rule must have an independent cleanup command ready before
injection. Prefer the helper (pre-arms `cleanup.sh`, no sshfs):

```bash
/root/venv/bin/python validation/backup_outage_check.py \
  --backup-ip 5.45.77.77 \
  --block-seconds 120 \
  --require-collector-pid "$(pgrep -f 'app/screaner_b_o.py' | head -1)"
```

Pass:

- writer heartbeat and publish latency remain stable;
- compactor continues producing final files;
- transfer watchdog terminates the stuck rclone process;
- backlog grows without loss and drains after connectivity returns.

### 3. Compactor kill/restart

Send `SIGKILL` while `.inprogress` exists, then rerun the one-shot compactor.

Pass:

- source files remain available;
- one valid deterministic consolidated final exists;
- no duplicate output;
- remaining source archive steps complete after restart.

### 4. Backlog recovery

Block backup traffic for ten minutes while writer and compactor continue.
Record backlog count and bytes every compaction cycle, restore connectivity,
and run transfer until backlog is zero.

Pass: backlog growth is bounded by local disk capacity and every pending file
transitions to `sent` without affecting writer or compactor.

## Stop conditions

Stop the experiment immediately if:

- free space on `/data` is insufficient for live + compacted + archive + sent;
- writer publish latency changes materially during transfer failure;
- any row-count mismatch occurs;
- the firewall DROP rule cannot be removed;
- watchdog leaves an rclone child process running;
- a source file is missing without a valid consolidated final.
