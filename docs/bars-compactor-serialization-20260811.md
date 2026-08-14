# Bars/compactor serialization fix — 2026-08-11

## Scope

Track 1 storage scheduling on production VPS `root@38.180.94.108`. This change
does not alter WebSocket ingest, parsing, spread calculation, trading, parquet
schema, or data lifecycle semantics.

## Root cause

The 16 GiB canary showed no OOM and healthy collector/remote progress, but
mutual systemd `Conflicts=` treated a new bars or compactor start as a request
to stop the peer. Long bars hive transfers therefore produced `status=15/TERM`
for both services and compaction lag above 100 minutes. `Conflicts=` is process
termination, not mutual exclusion.

## Rejected first serialization contract

- Shared storage-heavy lock: `/run/spread-heavy-storage.lock`.
- Tick compactor retains `/run/spread-compactor.lock`, waits up to 90 seconds
  for the shared lock, then runs exactly one window. A wait timeout emits
  `compactor_skipped_heavy_storage_busy` and exits 0; it never signals bars.
- Bars transfer originally held the same shared lock for its whole
  `--max-files=500` invocation. This was rejected: a 500-file run held it for
  at least 2 h 38 m and starved compaction.
- `BACKUP_TRANSFER_LOCK_PATH=/run/spread-bars-backup.lock` remains the
  independent bars-to-bars exclusion.

## Implemented serialization contract

At 15:29 UTC the VPS received the narrow `backup_transfer` change:

- Bars still retains `BACKUP_TRANSFER_LOCK_PATH=/run/spread-bars-backup.lock`
  for bars-to-bars invocation exclusion.
- Only bars sets
  `BACKUP_SHARED_LOCK_PATH=/run/spread-heavy-storage.lock`.
- The transfer process takes that shared lock immediately before one source
  file's existing `stat → copyto temporary → size verify → moveto final →
  final verify → local sent` lifecycle, then releases it. No verification,
  remote temporary-to-final transition, or source move was weakened.
- If compaction owns the shared lock at a file boundary, bars emits
  `transfer_deferred_shared_lock_busy`, leaves that source and all remaining
  sources pending, and exits 0 for the next timer tick. It does not silently
  skip a source.
- The compactor retains its 90-second wait. A waiting compactor now has a
  kernel lock boundary after each bars file, instead of waiting for a
  multi-hour batch.

This is the smallest safe production design. One-file systemd invocations
would add process startup and sqlite overhead to every bar, while unchanged
500-file work preserves existing catch-up behavior but removes starvation.

## Risks and observability

Bars may defer for one or more timer intervals during sustained compaction; its
active hive backlog count, size, oldest age, and shared-lock deferrals are reported by
`validation/ops_alerts.py`. A compactor wait can skip one cycle, so
`compaction_complete`, compaction lag, and `compactor_skipped_heavy_storage_busy`
must be observed together. `status=15/TERM` and OOM are separate signals.

This is host-local serialization only. It does not establish remote durability:
the validation must still demonstrate local lifecycle transitions and monotonic
remote tick/bars inventory.

## Validation gate

Focused proof: 60–90 minutes while the N≈337 lean+bars collector is active,
with no new conflict-driven TERM, `NRestarts=0`, regular
`compaction_complete`, and final/trending lag at or below 30 minutes. Confirm
tick and bars remote growth when work exists, plus sent and retention events.

Production is **not READY** until an actual 8–12-hour canary additionally has
OOM=0, TERM≤2/hour, final lag≤30 minutes, collector health, tick and bars
remote growth, lifecycle pruning, and remote bars inventory evidence covering
all 336 bars coins. A started but incomplete overnight is only partial proof.

## Final focused snapshot — 2026-08-11 15:25 UTC

Environment: production VPS `root@38.180.94.108`; production entrypoint remains
`spread-collector.service` (`MainPID=24505`). First materialization is under
`/data/live` and `/data/bars`; durable destinations are
`backup1tb:spread-compacted` and `backup1tb:spread-bars`.

The earlier SSH timeouts were not supported as host saturation: a bounded
follow-up completed `hostname`, `/proc/loadavg` (`0.48 0.43 0.49`), and
`systemctl` in about three seconds. TCP/22 and a minimal non-interactive SSH
command also completed. The timeout is therefore treated as an intermittent SSH
transport/session issue, not evidence of VPS or service saturation.

Observed snapshot:

- collector and all three timers were `active`; collector had `NRestarts=0`;
  62.38 GiB disk was free and the local spool/backlog was empty;
- OOM and `status=15/TERM` counts since the fix window (`12:47 UTC`) were both
  zero;
- the last `compaction_complete` and `archive_retention_complete` were at
  `12:47 UTC`. At `15:25 UTC`, `compaction_lag_minutes=260.355`, rising from
  `102.432` at baseline, and both lifecycle-complete alerts were stale;
- bars transfer remained live and successful (latest object completed in
  23.38 s), with zero observed `bars_transfer_skipped_busy`, but it held
  `/run/spread-heavy-storage.lock` for at least 2 h 38 m while processing its
  500-file batch. Bars outstanding was 48,871 files / 166.7 MiB, oldest
  1,115 minutes;
- tick remote inventory was 1,444 objects / 17,491,664,181 bytes. Bars remote
  inventory confirmed all 336 `base_coin` partitions. A bounded aggregate
  bars-size call did not complete, so it is not claimed as a tick-to-bars
  growth comparison. The successful per-object transfer is only progress
  evidence, not the required aggregate-growth proof.

## Focused-gate verdict

**NO-GO — overnight not started.** The collector is healthy and the
serialization patch has eliminated new conflict-driven TERM, but the strict
focused gate fails: compaction has not completed after the fix window, lag is
260 minutes and rising rather than at or below 30 minutes or demonstrably
falling, and archive retention is likewise stale. The long-running bars batch
holding the shared lock is the concrete blocking condition. Do not label this
runtime READY or start the 8–12-hour canary until a narrow scheduler remedy is
reviewed and a new focused window demonstrates regular compaction completion,
falling lag, and bounded bars backlog.

## Per-file lock rollout — observation in progress

At `2026-08-11 15:29 UTC`, the VPS deployment replaced the outer bars
`flock -n /run/spread-heavy-storage.lock` with
`BACKUP_SHARED_LOCK_PATH=/run/spread-heavy-storage.lock` passed to
`app.storage.backup_transfer`. The collector was not restarted.

The first intentional compactor exercise completed at `15:32:38 UTC` while
the new bars service was active:

- `compaction_complete` reported a 31.833 s, row-count-matched
  `2026-08-11T11:05Z–11:10Z` window;
- `archive_retention_complete` removed 41,657 eligible archived files;
- bars completed five fully verified/size-confirmed files in 119.010 s, then
  emitted `transfer_deferred_shared_lock_busy` at the next file boundary;
- its local pending hive sources remained present (`49,191` files /
  `167.795 MiB`), so the defer was observable, not a silent skip.

This establishes the intended lock handoff, but is not a focused-gate pass.
The durable 75-minute sampler at
`/var/log/spread/bars-compactor-focused-20260811T1530Z.log` must still show
regular compaction, final lag behavior, TERM/OOM/restart counts, lifecycle
events, and backup progress before a canary decision.

## Focused production gate after rollout — 15:31–16:35 UTC

Environment and boundary:

- VPS runtime command remains `spread-collector.service` /
  `app/screaner_b_o.py`; the collector was never stopped.
- First materialization is `/data/live` and `/data/bars`; local sent copies
  are retained under `/data/compacted/sent` and `/data/bars/sent`.
- Durable backup destinations are `backup1tb:spread-compacted` and
  `backup1tb:spread-bars`. A source moves to `sent` only after the existing
  remote final verification succeeds.

Evidence from the 64-minute durable sampler plus bounded VPS probes:

- The collector stayed `active` with `NRestarts=0`; its spool remained empty.
  There were zero OOM signatures and zero `status=15/TERM` outcomes from the
  post-restart baseline `15:29:30 UTC`. The deliberate bars restart at
  15:29:15 is excluded from that baseline and is not a conflict event.
- Compaction resumed on the normal five-minute cadence:
  `compaction_complete` and `archive_retention_complete` were 12–25 seconds
  old at sampled timer boundaries. The first handoff window had
  `row_count_match=true`; subsequent archive-retention cycles ran normally.
- This repaired starvation but did not produce catch-up. Lag was
  `262.910 min` immediately before rollout, was `262.391 min` at 15:57, and
  was `263.543 min` at 16:33. It stayed far above the 30-minute gate and
  finished slightly worse than baseline.
- Bars made real confirmed remote progress: 22 `transfer_result` successes
  after rollout, including remote final size verification and source-to-sent
  lifecycle. It also emitted controlled shared-lock deferrals at compactor
  boundaries. But active bars backlog grew from `48,864 / 166.679 MiB` to
  `51,800 / 176.695 MiB`; this is unbounded for the observed rate.
- Tick remote inventory grew from `1,444 / 17,491,664,181 bytes` to
  `1,467 / 17,738,599,498 bytes` (+23 objects, +246,935,317 bytes). Tick
  `sent` count was 110 at rollout and 109 during the focused run despite 12
  successful post-rollout transfers, demonstrating that retention pruning was
  active while archive-retention events also continued.
- `/data` free space moved from `62.384 GiB` to about `62.19 GiB`, with no
  ENOSPC or watchdog-kill signal. The existing remote bars inventory proof of
  all 336 `base_coin` partitions remains applicable; no partitioning code or
  remote prefix changed.

## Strict verdict after per-file lock rollout

**NO-GO — do not start the 8–12-hour canary.** Per-file locking demonstrably
prevents the former multi-hour shared-lock starvation: compaction now gets a
file boundary and bars defers rather than being SIGTERM-ed. However, the
focused gate fails its central acceptance condition: compaction lag remains
about 263 minutes and the bars backlog grows by roughly 2,936 files in 64
minutes. The current one-window-per-five-minute compactor has no observed
catch-up margin over the incoming workload.

Services and timers remain operating. The next narrow work item must quantify
and safely add compaction catch-up capacity without reintroducing concurrent
heavy storage work; it is separate from this lock-starvation fix.

## Catch-up capacity decision — 2026-08-11

The serialization fix is retained. The remaining failure is scheduler capacity,
not a shared-lock starvation or memory failure:

- Current normal windows contain 1,008–1,680 source files, 300,000–500,000
  rows, and 16.5–27.8 MiB of input. Their measured `compaction_duration_ms` is
  16.1–31.8 s (the 700,000-row outlier was 43.9 s), with row-count checks
  passing.
- With `OnUnitActiveSec=5min` and `--max-windows 1`, exactly one five-minute
  source window is completed per five-minute cycle. That matches fresh-window
  arrival but cannot reduce the approximately 263-minute historical queue.
  The 16–44 second execution time is idle for most of the five-minute cadence;
  it is not the limiting work rate.
- A `--max-windows >1` batch would increase catch-up capacity but keeps Arrow
  allocations and the heavy-storage lock across multiple windows. It is not
  needed for the measured bottleneck and weakens the existing 2.5 GiB RSS
  safety boundary.

The selected production setting is `OnUnitActiveSec=2min`,
`AccuracySec=15s`, and the existing `--max-windows 1`, `MemoryMax=2500M`,
90-second bounded shared-lock wait, and per-file bars lock. At one completed
five-minute window per two-minute slot, nominal capacity is 2.5 source windows
per five minutes against one arriving source window: net catch-up capacity is
1.5 windows / 5 min (7.5 minutes of source time / 5 minutes wall time). The
normal 44-second maximum leaves at least 76 seconds of slot headroom; a rare
90-second bars wait may miss a slot but cannot cause overlapping compactor
processes or unbounded per-process RSS.

The lag alert remains required until the slope is negative and the lag is at
most 30 minutes. The focused VPS sampler must record timestamped lag snapshots,
`compaction_complete` cadence and window ends, bars backlog/count/age, disk
free space, OOM/TERM journal counts, collector `NRestarts`, remote tick/bars
inventory, and archive/sent lifecycle events. Do not start the 8–12-hour
canary merely because the scheduler setting is deployed.

## Throughput bridge approved for validation — 2026-08-12

The per-file lock removed starvation but did not remove the dominant bars cost:
one 3.5 KiB object took approximately 22.7 seconds across distinct `rclone`
`stat`, `copyto`, temporary-size, `moveto`, and final-size subprocesses. This
is far below the approximately 4,044 files/hour ingestion rate.

The bridge keeps each bar's atomic lifecycle but processes at most 32 hive
files under one acquisition of `/run/spread-heavy-storage.lock`. It starts one
local `rclone rcd` daemon bound only to a temporary Unix socket and sends
individual RC lifecycle calls through its reusable SFTP backend:

```text
per file: final stat → copy to .inprogress → temporary stat
          → move to final → final stat → manifest confirmed → local sent move
per micro-batch: acquire heavy lock → one rclone rcd/SFTP backend → ≤32 files
                 → release heavy lock
```

Thus the mechanism is not a lock wrapper around the old five SFTP subprocesses
per file. For microfiles, existing size-only SHA policy remains unchanged; final
remote size confirmation remains mandatory for every file. A contended shared
lock gets three 0.5-second retries, records structured deferral, and skips only
that bounded batch rather than ending all 500 candidates. Compactor retains its
90-second priority wait.

This is a candidate production bridge, not a readiness claim. The focused
N=337 proof must measure actual batch duration, drain rate, bars backlog/age
slope, compactor lag, and exact remote freshness before an overnight canary.
