# Production unit snippets

Staging/prod code root: `/root/spread_staging`  
Python: `/root/venv/bin/python`

## Absolute environment (lean production)

```bash
WorkingDirectory=/root/spread_staging
Environment=SPREAD_PARQUET_ROOT=/data/live
Environment=SPREAD_BARS_ROOT=/data/bars
Environment=SPREAD_RUNTIME_LOG=/var/log/spread/runtime.log
Environment=SPREAD_FAILED_BATCHES_LOG=/var/log/spread/failed_batches.log
Environment=SPREAD_SPOOL_ROOT=/data/spool
Environment=SPREAD_LEAN_SCHEMA=1
Environment=SPREAD_COLLECT_BARS=0
Environment=BACKUP_RCLONE_BINARY=/opt/rclone-1.74.4/rclone
Environment=BACKUP_RCLONE_REMOTE=backup1tb
Environment=BACKUP_RCLONE_PATH=spread-compacted
Environment=BACKUP_SFTP_KEY_PATH=/root/.ssh/id_ed25519_uploader
Environment=BACKUP_RCLONE_SFTP_CONCURRENCY=8
Environment=BACKUP_RCLONE_SFTP_CHUNK_SIZE=128k
```

Do not use rclone concurrency=32 / chunk=512k on this endpoint.

Optional soak / slice overrides (not in the unit by default):

```bash
Environment=SPREAD_ROW_START=0
Environment=SPREAD_ROW_END=10
Environment=SPREAD_PERSIST_EVERY=500
Environment=SPREAD_BAR_PERSIST_EVERY=50
```

## Isolated lean soak (before prod cutover)

Do **not** write lean into leftover canary day partitions under `/data/live` without a clear cutover. Prefer:

```bash
mkdir -p /data/experiments/lean_soak/{live,bars,spool} /var/log/spread
cd /root/spread_staging
SPREAD_LEAN_SCHEMA=1 \
SPREAD_COLLECT_BARS=0 \
SPREAD_PARQUET_ROOT=/data/experiments/lean_soak/live \
SPREAD_BARS_ROOT=/data/experiments/lean_soak/bars \
SPREAD_SPOOL_ROOT=/data/experiments/lean_soak/spool \
SPREAD_RUNTIME_LOG=/var/log/spread/lean_soak_runtime.log \
SPREAD_FAILED_BATCHES_LOG=/var/log/spread/lean_soak_failed.log \
SPREAD_ROW_END=5 \
SPREAD_PERSIST_EVERY=200 \
SPREAD_BAR_PERSIST_EVERY=20 \
/root/venv/bin/python app/screaner_b_o.py
```

Stop with `SIGTERM` (exit 0 expected after drain).

## systemd install

```bash
mkdir -p /var/log/spread /data/live /data/bars /data/spool /data/compacted
cp /root/spread_staging/deploy/systemd/spread-*.service \
   /root/spread_staging/deploy/systemd/spread-*.timer \
   /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now spread-collector.service
systemctl enable --now spread-compactor.timer
systemctl enable --now spread-backup-transfer.timer
systemctl status spread-collector.service --no-pager
systemctl list-timers 'spread-*' --no-pager
```

## Enable lean accumulation tomorrow (cutover)

Prerequisites: soak PASS, disk headroom after archive retention, no active v1 writer on the same tick root.

```bash
# 1) Deploy code + unit (lean flags already in spread-collector.service)
cd /root/spread_staging
cp deploy/systemd/spread-collector.service /etc/systemd/system/
systemctl daemon-reload

# 2) Ensure roots exist; prefer empty/new event_date under /data/live
mkdir -p /data/live /data/bars /data/spool /data/compacted /data/gaps /var/log/spread

# 3) Start collector
systemctl enable --now spread-collector.service
systemctl status spread-collector.service --no-pager

# 4) Confirm flags in first log lines
grep -E 'runtime_paths|schema_mode' /var/log/spread/runtime.log | tail -5
```

Rollback to v1 (emergency):

```bash
# Edit unit: set SPREAD_LEAN_SCHEMA=0 and SPREAD_COLLECT_BARS=0 (or remove)
systemctl daemon-reload
systemctl restart spread-collector.service
```

## cron alternative

```bash
cp /root/spread_staging/deploy/cron/spread-maintenance.cron \
  /etc/cron.d/spread-maintenance
chmod 644 /etc/cron.d/spread-maintenance
```

Compactor uses `flock /run/spread-compactor.lock`.  
Backup transfer uses internal lock `/run/spread-backup.lock` (offset schedule `2-59/5`).

## Ops alerts

```bash
/root/venv/bin/python /root/spread_staging/validation/ops_alerts.py --once
```

## Related

- Runbook: `docs/compaction-backup-runbook.md`
- Units: `deploy/systemd/`
- Cron: `deploy/cron/spread-maintenance.cron`
- Lean contract: `docs/storage-contract.md`, `docs/local-lean-collector.md`
