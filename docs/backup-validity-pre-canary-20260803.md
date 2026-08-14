# Backup validity pre-canary: 2026-08-03

> **Host migration (2026-08-10):** current production collector host is `root@38.180.94.108`. The IP below is the historical host for this report.

## 1. Pipeline block

```text
VPS collector → /data/live → compact → /data/compacted → rclone SFTP → backup1tb
```

Contexts distinguished:

| Context | Role in this check |
|---|---|
| Local Mac repo | docs / validation script only |
| VPS `root@38.244.198.42` | runtime, local compacted/sent, manifests |
| Remote `backup1tb` | durable backup under check (rclone, no sshfs) |

Runtime still active during check:

- collector pid `298870` (`app/screaner_b_o.py`) — not killed
- timers: `spread-compactor.timer`, `spread-backup-transfer.timer`

## 2. Existing files/modules involved

- `validation/canary_24h.py` (`status` / `account`)
- `validation/ops_alerts.py`
- `app/storage/backup_transfer.py` (transfer sqlite; download SHA verify on upload)
- soak evidence: `/data/experiments/prod_soak_20260803_122023/`
- canary evidence: `/data/experiments/canary_24h/`, live paths `/data/live`, `/data/compacted`
- rclone: `/opt/rclone-1.74.4/rclone`, key `/root/.ssh/id_ed25519_uploader`

## 3. Candidate interpretations

| Interpretation | Evidence |
|---|---|
| A. Soak backup on `backup1tb:prod-soak-20260803_122023` is byte/row-valid | Confirmed: 13/13 size+SHA-256+row matches |
| B. Canary writes to `backup1tb:spread-canary-24h` as status file claims | **Rejected**: remote dir missing; systemd uses `BACKUP_RCLONE_PATH=spread-compacted` |
| C. Canary data on `backup1tb:spread-compacted` is byte/row-valid | Confirmed for all transferred files checked |
| D. Schema missing `event_date` means corruption | **Rejected**: writer drops `event_date` before parquet write; hive path carries date |

## 4. Key risks and failure modes

| Risk | Status |
|---|---|
| Silent remote corruption / truncated upload | Not observed — download SHA == local SHA == manifest `output_sha256` |
| Local success mistaken for remote success | Avoided — every checked file re-downloaded via rclone |
| Canary accounting remote-row delta | Tool reports file count only (`transfers` has no `row_count`); use download verify |
| Status/path drift (`spread-canary-24h` vs `spread-compacted`) | Operational mismatch — does not invalidate data on actual prefix |
| Disk pressure (`/` ≈ 5.6 GiB free, large archives) | Residual ops risk for full 24h (see residual hardening note) |
| In-flight compacted file not yet remote | Expected under 5-minute transfer cadence; observed then resolved |

## 5. Minimal experiment executed (read-only)

1. Inventory remotes: `rclone lsd/lsl` for soak + canary-related prefixes.
2. Compare transfer sqlite sizes vs remote sizes vs local `sent/` sizes.
3. For each matched file: rclone `copyto` to `/tmp/backup_validity_20260803`, SHA-256, parquet row count, schema column list; delete download after check.
4. Re-check the one canary file that was mid-transfer during the first inventory.

No remote deletes. No collector kill. No dataset cleanup.

## 6. VPS/storage validation results

### Remote layout

| Remote prefix | Present | Notes |
|---|---|---|
| `backup1tb:prod-soak-20260803_122023` | yes | soak acceptance dataset |
| `backup1tb:spread-compacted` | yes | **actual** canary/prod transfer target |
| `backup1tb:spread-canary-24h` | **no** | status file path only; directory not found |

### Soak (`prod-soak-20260803_122023`)

| Metric | Value |
|---|---|
| Remote files | 13 |
| Remote bytes | 280,166,201 |
| Transfer sqlite `sent` | 13 / 280,166,201 |
| Size matches (local/sqlite/remote) | **13/13** |
| Download SHA + row matches | **13/13** |
| Manifest / verified rows | **5,458,497 / 5,458,497** |
| Missing remote / local | 0 / 0 |
| Checksum failures | 0 |

### Canary / prod path (`spread-compacted`)

| Metric | Value |
|---|---|
| Files verified (download) | 7 |
| Verified bytes (sum of checked) | 323,024,776 |
| Size matches for checked set | **7/7** |
| Download SHA + row matches | **7/7** |
| Verified rows | **6,763,683** (6 files at first pass) + **863,683** (7th after transfer) = **7,627,366** |
| First-pass missing remote | 1 file in flight → later appeared and verified |
| Schema | 25 columns; `event_date` absent by design |

### Spot-check schema (both soak + canary samples)

Readable parquet; columns:

`event_dt`, `event_local_ts_ms`, `base_coin`, `trigger`, `spread_long`, `spread_short`, latency/freshness fields, book fields — matches writer keep-cols after dropping partition `event_date`.

### Ops snapshot during check

- `canary_24h.py --action status`: `state=running`, pid alive
- `ops_alerts.py --once`: `ops_alert_ok` at check time (~5.6 GiB free)
- Local canary accounting: `manifest_rows == local_output_rows` (delta 0); remote row accounting unavailable in sqlite schema

## 7. Success criteria

| Criterion | Result |
|---|---|
| Soak remote inventory matches transfer/manifest set | **pass** |
| Soak download SHA + rows match local + manifest | **pass** |
| Canary remote data readable and matches local/manifest for transferred files | **pass** |
| No silent-loss signal on confirmed transfers | **pass** |
| Safe to continue already-running 24h canary from a backup-integrity standpoint | **GO WITH CONDITIONS** |

## 8. Recommended next step / GO-NOGO

**GO WITH CONDITIONS** — continue the running 24h canary.

Conditions (operational, not backup corruption):

1. Treat durable canary backup prefix as `backup1tb:spread-compacted`, not `spread-canary-24h`.
2. Keep watching disk free space / archive growth; do not claim unconditional READY until wall-clock canary accounting completes.
3. Prefer download-verify / size+SHA checks for remote truth; do not rely on `canary_24h.py --action account` remote row deltas until `transfers.row_count` exists.

Evidence artifact on VPS: `/tmp/backup_validity_20260803/result.json` (first-pass JSON; 7th canary file verified in follow-up).
