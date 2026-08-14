# Backup validity post canary early-stop — 2026-08-04

> **Host migration (2026-08-10):** current production collector host is `root@38.180.94.108`. The IP below is the historical host for this report.

## Verdict

**GO WITH CONDITIONS** (backup integrity on `backup1tb:spread-compacted`)

Remote canary compacted dataset is **fully** inventory-complete: all **266** files re-downloaded; SHA-256 + parquet row counts match manifests (**119,543,737** rows / **6,078,570,384** bytes). Local `sent/` retention still makes local accounting look broken — that is **not** remote loss.

Unconditional production **READY** remains blocked for reasons in `docs/canary-24h-early-stop-20260804-result.md` (alert storm, accounting tools, disk, early stop). This document only answers: *is the backup intact?*

## 1. Pipeline block

| Item | Value |
|---|---|
| Host | `root@38.244.198.42` |
| Code root | `/root/spread_staging` |
| Compacted local | `/data/compacted` (+ `sent/`) |
| Manifests / sqlite | `/data/compacted/.state` |
| Durable remote | **`backup1tb:spread-compacted`** |
| Checker | `validation/check_backup_validity.py` (extended for retention) |
| Evidence on VPS | `/tmp/backup_validity_post_canary_20260804/` |
| Collector | **not restarted** (inactive) |
| Timers | compactor / backup-transfer left running |

## 2. What was checked

1. Full remote inventory via `rclone lsl` vs complete manifests + transfer sqlite sizes.
2. Stratified sample **48/266**, then **full download of all 266** remote parquet files: SHA-256 + row count vs manifest (`output_sha256`, `total_rows`); schema body columns vs `SPREAD_EVENT_BODY_COLS`.
3. Explicit split of **local retained-away** vs **true missing remote**.
4. Non-destructive: downloads under `/tmp/...` deleted after each file; no remote deletes.

Script change (needed for post-retention truth): missing local `sent/` no longer fails inventory; `--download` verifies `remote+manifest` when local is gone; `--sample N` for stratified large samples.

## 3. Evidence table

| Metric | Value | Notes |
|---|---|---|
| Checked at (full) | `2026-08-04T12:45:30Z` | UTC; sample48 earlier `12:01:40Z` |
| Remote files | **266** | Was 265 at first inventory; 1 post-stop backlog then transferred |
| Remote bytes | **6,078,570,384** (~5.661 GiB) | Equals verified download bytes |
| Complete manifests | **266** | |
| Manifest rows sum | **119,543,737** | Equals canary `shutdown_flush_done` published rows |
| Transfer sqlite `sent` | **266** | `confirmed=0` (schema unchanged) |
| Remote size OK | **266 / 266** | vs transfer (+ local when present) |
| Missing remote | **0** | After backlog cleared |
| Size mismatches | **0** | |
| Extra remote (unexpected) | **0** | |
| Local present (at full) | **143** | `compacted/` + `sent/` |
| Local retained-away | **123** | On remote; erased locally by retention |
| Download verify | **266 / 266 OK** | 128 remote-only + 138 local+remote |
| SHA match | **266 / 266** | remote == manifest (+ local when present) |
| Row match | **266 / 266** | verified rows sum == manifest rows sum |
| Schema gaps | **0** | Full v1 body cols present |

### First inventory snapshot (before backlog transfer)

At `~11:52Z`, one window was still local-only:

| File | Local | Remote | Transfer | Interpretation |
|---|---|---|---|---|
| `spread_20260804T114000Z_…114500Z.parquet` | 8,559,444 B / 143,737 rows | missing | not sent | Post-stop compaction product; **not canary loss** |

By sample48 (`12:01Z`) that file was on remote and verified (`ok=true`). Full pass later confirmed all 266.

### Verify modes (retention distinction)

| Mode | Sample48 | Full | Meaning |
|---|---|---|---|
| `remote+manifest` | 24 | **128** | Local `sent/` erased; remote SHA/rows match manifest |
| `local+remote+manifest` | 24 | **138** | Local copy still present; triple match |

## 4. Candidate interpretations

| Interpretation | Status |
|---|---|
| A. Remote backup corrupted / truncated | **Rejected** — 266/266 size + SHA + rows |
| B. Local `row_delta` / missing outputs mean data loss | **Rejected** — 123 “missing local” are remote-present retention erasures |
| C. Canary remote inventory incomplete at early-stop | **Was transient** — 1 post-stop backlog; cleared by timer within ~10m |
| D. Backup integrity proven for READY | **Not claimed** — READY has separate blockers |

## 5. Key risks / remaining conditions

1. **Local accounting tools** still report huge negative deltas once `sent/` is retained away — operators must prefer remote + manifest.
2. **Transfer schema** still `sent` only (no `confirmed` / `row_count`) — first-class remote row ledger still missing (download proof fills the gap).
3. Broader canary READY blockers (FNF alert storm, disk, ops_alerts parse, early stop) are unchanged.
4. Compactor/backup timers still active post-collector stop — expected to keep clearing archive tails; watch disk.

## 6. Success criteria

| Criterion | Result |
|---|---|
| Every complete manifest window present on remote with size OK | **pass** (266/266) |
| Full download SHA == manifest | **pass** (266/266) |
| Full parquet rows == manifest | **pass** (266/266; 119,543,737) |
| Distinguishes retention vs true loss | **pass** (123 retained-away documented) |
| No destructive ops / no collector restart | **pass** |

## 7. Recommended next step

1. Treat **`backup1tb:spread-compacted` canary set as intact** for research recovery.
2. Do **not** enable `spread-collector.service` from this alone — finish READY blockers from early-stop doc.
3. Prefer remote+manifest in accounting/alerts after `sent/` retention.
4. Optional: keep `full.json` on VPS as soak evidence; no further download needed unless remote changes.

## 8. Artifacts

| Path | Role |
|---|---|
| VPS `/tmp/backup_validity_post_canary_20260804/inventory.json` | First inventory (265 + 1 backlog) |
| VPS `/tmp/backup_validity_post_canary_20260804/sample48.json` | Stratified SHA/row proof |
| VPS `/tmp/backup_validity_post_canary_20260804/full.json` | **Full 266/266 download proof** |
| Repo `validation/check_backup_validity.py` | Retention-aware checker |
| Canvas `canvases/backup-integrity-post-canary-20260804.canvas.tsx` | Russian dashboard |
