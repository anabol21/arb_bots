# OLD VPS RAM audit — 2026-08-11

## Scope and environment

- Track (D) latency / runtime-validation evidence only.
- Host inspected over SSH: `root@38.244.198.42` (`1.9 GiB` RAM, no swap).
- Read-only inspection window: `2026-08-11T15:31:04Z`–`15:32:41Z`.
- No production collector was active. No mounts, services, collector, compactor, backup,
  retention, or disk-reclaim configuration was changed.

## Host snapshots

| Time (UTC) | Used | Free | Available | Swap |
|---|---:|---:|---:|---:|
| 15:31 (before audit) | 1.4 GiB | 96 MiB | 336 MiB | 0 B |
| 15:32 (after audit) | 1.3 GiB | 164 MiB | 390 MiB | 0 B |

The small difference is ordinary cache/reclaim variation: no process was stopped during this
audit. `vmstat 1 3` during the first snapshot showed 20–22% I/O wait while compaction was
active; there was no swapping.

## Material consumers and ownership

| Consumer | PID / unit | RSS or cgroup memory | Classification | Action |
|---|---|---:|---|---|
| Parquet compactor | PID `1293376`, `spread-compactor.service` | 1,046–1,095 MiB RSS; 1.1–1.2 GiB cgroup | Neighbor-owned Track (D) compaction; protected | Not stopped |
| Backup transfer while active | PID `1293446`, `spread-backup-transfer.service` | 18.7 MiB RSS; its `rclone` child PID `1293545`: 69.4 MiB RSS | Neighbor-owned Track (D) backup; protected | Not stopped; inactive by 15:32 |
| Firmware daemon | PID `329390`, `fwupd.service` | 130.6 MiB RSS; 126.8 MiB cgroup | System | Not stopped |
| Remaining OS services | `multipathd`, `snapd`, journals, SSH, etc. | each at most 27.1 MiB RSS | System | Not stopped |
| Detached `screen` | PID `185199`, session `185199.spreads` | 1.1 MiB RSS | Unidentified, not a latency process | Not stopped |

The compactor command was:

```text
/root/venv/bin/python -m app.storage.compactor --live /data/live --compacted /data/compacted --interval 300 --retention-hours 12 --max-windows 1
```

At `15:32:41Z` it remained active/starting under its timer, with `1,095,160 KiB` RSS and
`1,258,196,992` bytes cgroup memory. Its anonymous RSS was `1,067,920 KiB`; this is the direct
explanation for the low available RAM rather than filesystem cache alone.

## Latency-artifact check

- No live process command line or active/transient systemd unit matched `latency`, `E2`, `E2b`,
  `hostcmp`, `dose`, `ping_okx_bybit`, `shadow`, or `screaner_b_o`.
- The `screen` session has only an idle `/bin/bash` child, not a latency wrapper or Python
  child. It is therefore not evidence of a running old latency arm.
- No matching latency artifact was found under `/var/log/spread` or `/root`.
- **Stopped latency processes: none.** There was no clearly stale, latency-owned process or unit
  that could be stopped within this chat's ownership boundary.

## Verdict

`N=100` is **not safe to launch on OLD under current ownership**. Available memory was only
`336–390 MiB` during the audit, while the protected compactor alone held about `1.0 GiB` RSS on a
`1.9 GiB` host. The prior `blocked_capacity` decision remains valid. Reassess only after the
compaction/backup owner has completed its work and an independent fresh OLD-host memory snapshot
shows a sufficient margin; this latency chat must not stop the compactor to create that margin.


## User-requested temporary service stop (2026-08-11)

This section records a **user-requested temporary stop** to free memory before a future,
isolated latency `N=100` experiment. It is not a unit, timer, configuration, mount, retention,
or data-management change. No experiment was started.

### Exact action and evidence

- Pre-stop state captured at `2026-08-11T15:50:11Z` over SSH on
  `root@38.244.198.42`:
  - `spread-compactor.service` was `activating` with main PID `1293909`:
    `/usr/bin/flock -n /run/spread-compactor.lock /root/venv/bin/python -m
    app.storage.compactor --live /data/live --compacted /data/compacted --interval 300
    --retention-hours 12 --max-windows 1`.
  - `spread-backup-transfer.service` was `inactive` (main PID `0`).
  - `spread-bars-backup-transfer.service` was `inactive` (main PID `0`).
  - No `rclone` process was present, so no backup-transfer unit was stopped.
- Executed `systemctl stop spread-compactor.service` at approximately
  `2026-08-11T15:50:21Z`. Systemd confirmed PID `1293909` received `SIGTERM`; no
  unrelated process was killed. The transient `failed (Result: signal)` state caused by the
  expected termination was cleared with `systemctl reset-failed spread-compactor.service`;
  this does not change unit files or configuration.
- Final verification at `2026-08-11T15:50:58Z`:
  - `spread-compactor.service`, `spread-backup-transfer.service`, and
    `spread-bars-backup-transfer.service` were all `inactive`, each with main PID `0`.
  - No `rclone` process remained.
  - `spread-collector.service` remained `inactive`; it was not manipulated.

| Snapshot (UTC) | Used | Free | Available | Swap |
|---|---:|---:|---:|---:|
| Pre-stop 15:50:11 | 1.3 GiB | 194 MiB | 398 MiB | 0 B |
| Final 15:50:58 | 342 MiB | 1.2 GiB | 1.4 GiB | 0 B |

The compactor timer remains `active` and `enabled`, intentionally unchanged per scope. It can
start the service again on its scheduled trigger. To resume explicitly (not executed), use
`systemctl start spread-compactor.service`; backup units can be run explicitly with
`systemctl start spread-backup-transfer.service` and/or
`systemctl start spread-bars-backup-transfer.service` if needed.
