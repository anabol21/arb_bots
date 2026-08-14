# Deploy packaging

Artifacts for VPS production/staging packaging:

| Path | Role |
|---|---|
| `deploy/systemd/spread-collector.service` | Long-running collector |
| `deploy/systemd/spread-compactor.service` + `.timer` | Compactor every 5m under flock |
| `deploy/systemd/spread-backup-transfer.service` + `.timer` | Tick compacted transfer every 5m (offset) |
| `deploy/systemd/spread-bars-backup-transfer.service` + `.timer` | Bars hive → `backup1tb:spread-bars` every 5m |
| `deploy/cron/spread-maintenance.cron` | Cron alternative + ops alerts |
| `deploy/logrotate/spread` | Rotate `/var/log/spread/*` (copytruncate) |
| `docs/prod-unit-snippets.md` | Short install notes |
| `validation/ops_alerts.py` | Disk/backlog/compactor alerts |
| `validation/backup_outage_check.py` | Backup-only network fault check |
| `validation/canary_24h.py` | 24h canary launch/status/account |

Copy units only after acceptance soak is green. Prefer systemd timers; cron is a fallback.

Unattended notes (2026-08-05): collector uses `Restart=always`, `LimitNOFILE=65535`;
compactor archive retention defaults to **12h** in the unit (was 24h); sent retention stays 12h.
See `docs/unattended-readiness-20260805.md`.
