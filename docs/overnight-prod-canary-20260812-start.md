# Overnight production canary — старт (2026-08-12)

## 1. Pipeline block

Collection and storage: VPS runtime → local lifecycle → compaction → tick and
bars backup observation. The collector, ingest, parsing, spread calculation,
and trading logic were not changed.

## 2. Execution identity

- VPS: `root@38.180.94.108` (`a845945761.local`), runtime staging
  `/root/spread_staging`.
- Supervisor: transient systemd `overnight-prod-canary-20260812.service`,
  executing `/tmp/overnight-prod-canary-20260812.sh`.
- Evidence log: `/tmp/overnight-prod-canary-20260812.log`.
- Start: `2026-08-11T23:15:51Z` / `2026-08-12T02:15:51+03:00`
  (Europe/Moscow).
- Planned end: `2026-08-12T07:15:51Z` / `2026-08-12T10:15:51+03:00`.
- Duration and cadence: exactly 28,800 seconds (8 hours), baseline plus
  30-minute snapshots. The service is independent of the SSH client; its
  temporary `TimeoutStartSec=8h15min` is only a guard for final logging.

The unrelated failed `dose-n-supervisor-20260811c.service`
(`Result=exit-code`, `ActiveState=failed`) is not used.

## 3. Baseline gate at start

- `spread-collector.service`, `spread-compactor.timer`,
  `spread-backup-transfer.timer`, and
  `spread-bars-backup-transfer.timer`: active.
- Collector PID `24505`; `NRestarts=0`; lean heartbeat reports 337 pairs,
  bars enabled, `published_rows=119700000`, `bar_published_rows=98000`,
  and no collector write failures.
- `ops_alert_ok`; compaction lag 6.1 minutes, last completion about 217
  seconds before the snapshot; primary backlog 1 file / 12.0 MiB; spool 0.
- `MemAvailable` about 13–14 GiB; `/data`/`/` free about 60.6 GiB.
- Snapshot OOM and TERM markers since canary start: 0 / 0. The prior
  read-only window contained one bars `status=15/TERM` at `15:29:15Z`;
  it predates the canary and is not masked by its counters.

## 4. Observations collected

Each snapshot records units and timers, collector PID/restarts, `ops_alerts`,
compaction lag/events, OOM/TERM journal markers, memory and disk, local
live/archive/compacted/sent/bars/spool counts and sizes, and the collector
publish heartbeat.

The durable remote boundary remains `backup1tb:spread-compacted` for ticks
and `backup1tb:spread-bars` for bars. This canary observes it; it does not
change remote contents.

## 5. Bounded remote inventory and growth method

The baseline deliberately avoids a global `rclone size backup1tb:spread-bars`,
which previously exceeded 20 seconds:

1. `rclone lsf backup1tb:spread-bars/bar_5m --dirs-only --max-depth 1`
   completed under a 25-second outer bound and found **336**
   `base_coin=` directories, matching the 336 local base coins.
2. The canary records the count of top-level tick objects
   (`spread-compacted`, max depth 1) and the base-coin directory count at
   every snapshot.
3. For deterministic samples `BTC`, `ETH`, and `SOL`, it lists only
   `base_coin=<coin>` directory entries, selects the lexically newest
   `event_date=` partition, then counts files in that one partition. The
   baseline was `event_date=2026-08-05`: BTC 42, ETH 44, SOL 42 files.

Limitations: directory coverage proves neither parquet readability nor
freshness. The three-partition sample can miss a failure affecting another
coin, and an unchanged count is not a universal proof of no remote growth.
The final verdict additionally requires observed remote growth and the local
bars backlog/sent trend to agree; a global remote size is intentionally not
run during the canary.

## 6. Final READY condition

Status is **RUNNING, not READY**. At planned completion, Validation must
confirm the full 8-hour evidence window: collector continuity, zero new OOM,
no TERM thrash, compaction recovery/lag, stable disk, and monotonic remote
tick and bars indicators consistent with local sent/backlog transitions.
The existing bars backlog of 69,525 files / 237.2 MiB with oldest age about
1,586 minutes is a specific condition to resolve or explain before any
durability/READY claim.
