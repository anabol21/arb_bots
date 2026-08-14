# Residual hardening status: 2026-08-03

## Alerts

Tool: `validation/ops_alerts.py`

Covers:

- free space on `/data`
- archive age/count
- compacted backlog files/MB
- spool backlog
- `transfer_watchdog_kills`
- recent `compaction_alert`

Cron/systemd packaging wires `--once` every 5 minutes via
`deploy/cron/spread-maintenance.cron`. Compactor/transfer unit stdout appends to
`/var/log/spread/compactor.log` and `/var/log/spread/backup-transfer.log`.

## Backup-only outage check

Evidence: `/data/experiments/backup_outage_check_20260803/outage-check.jsonl`

| Check | Result |
|---|---|
| Pre-armed cleanup | pass |
| Block only `5.45.77.77` | pass |
| Writer heartbeat during block | pass (`writer_heartbeat_ok=true`) |
| Collector stayed alive | pass (pid 298870) |
| Backlog preserved during block | pass (2 files / ~95 MB) |
| Cleanup removed DROP rule | pass (`no_backup_drop_rules`) |
| Backlog drained after restore | pass (`pending_after=0`, `sent_after=2`) |
| Exit code | **0** |

No sshfs used.

## 24h canary

Launched at `2026-08-03T13:34:36Z` on production paths:

- live `/data/live`
- spool `/data/spool`
- compacted `/data/compacted`
- runtime `/var/log/spread/runtime.log`
- remote **actually** `backup1tb:spread-compacted` (systemd `BACKUP_RCLONE_PATH`;
  canary-status.json may still say `spread-canary-24h` — that prefix does not exist)
- collector pid `298870`
- expected end epoch from status file (~24h → `2026-08-04T13:34:36Z`)

Status/accounting:

```bash
/root/venv/bin/python /root/spread_staging/validation/canary_24h.py --action status
/root/venv/bin/python /root/spread_staging/validation/canary_24h.py --action account \
  --remote backup1tb --remote-path spread-compacted
```

Maintenance timers enabled:

- `spread-compactor.timer`
- `spread-backup-transfer.timer`

Collector service unit is installed but not enabled (canary owns the process).

### Disk condition (resolved mid-canary)

At launch, `/` had ~5–7 GiB free while `/data/experiments` held ~16 GiB of
prior experiments (notably `prod_soak_20260730_161700` ≈ 12 GiB). Without
cleanup, canary archive/sent growth would have filled `/` before first
retention erase (sent 12h, archive 24h).

**2026-08-03 ~14:45 UTC:** operator-approved delete of old experiment dirs only
(see `docs/canary-24h.md` § Disk cleanup). Free space **4.8 GiB → ~20 GiB**.
Kept `canary_24h` + production paths; collector pid `298870` unharmed.

Remaining risk: archive growth ~1.3 GiB/h may still pressure disk before 24h
archive retention — watch `df -h /`.

## Readiness note

Acceptance 1h soak: PASS.  
Residual tooling + outage check + canary launch: DONE.  
Mid-canary disk cleanup: DONE (experiments only).  
Unconditional READY after 24h wall-clock accounting: **not yet claimed**.
