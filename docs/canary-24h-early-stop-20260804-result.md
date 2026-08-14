# 24h canary early-stop result — 2026-08-04

> **Host migration (2026-08-10):** current production collector host is `root@38.180.94.108`. The IP below is the historical host for this report.

## Verdict

**PASS WITH CONDITIONS**

Unconditional **READY is still blocked**.

Operator-requested early stop at ~T+22.1h (~1.9h before planned end). Remaining wall-clock would not have changed the main picture: collector + compaction + remote backup were healthy, but local accounting after `sent/` retention and the compaction stale-path alert storm already block READY.

## 1. Pipeline block

| Item | Value |
|---|---|
| Host | `root@38.244.198.42` |
| Code root | `/root/spread_staging` |
| Collector | `/root/venv/bin/python app/screaner_b_o.py` |
| Pid | `298870` (same pid entire run) |
| Live | `/data/live` |
| Spool | `/data/spool` |
| Compacted | `/data/compacted` (+ `sent/`) |
| Runtime log | `/var/log/spread/runtime.log` |
| Durable remote | **`backup1tb:spread-compacted`** |
| Status-file remote label | `spread-canary-24h` (stale / incorrect) |
| Canary status | `/data/experiments/canary_24h/canary-status.json` → `early_stopped` |

## 2. Shutdown evidence

| Item | Value |
|---|---|
| Signal | **SIGINT** (graceful; no SIGTERM/SIGKILL) |
| Sent at | `2026-08-04T11:40:34Z` |
| Process gone | after **7s** (`2026-08-04T11:40:40Z`) |
| Runtime path | `shutdown signal received` → cancel listeners → `flushing opportunities buffer` → `publisher_shutdown_begin \| queue_depth=0 \| mount_dead=False` → drain publishes → **`shutdown_flush_done`** |
| Flush totals | `published_files=402192`, `published_rows=119543737`, `bytes_written=12966186805`, `published_jobs=1196` |
| Exit code | **not captured** (launched with `start_new_session=True`, reaped by pid 1). Code path is the production expected-signal exit-0 path; last runtime event is successful flush. Console log ends on `shutdown_flush_done`. |

## 3. Elapsed vs planned

| Item | Value |
|---|---|
| Launched | `2026-08-03T13:34:36Z` |
| Planned end | `2026-08-04T13:34:36Z` (24.0h) |
| Stopped | `2026-08-04T11:40:40Z` (process) / status `stopped_at` `11:41:01Z` |
| Elapsed | **~22.10 h** |
| Remaining vs plan | **~1.90 h** |
| Forced kill | no |

## 4. Key metrics

| Metric | Value | Notes |
|---|---|---|
| Collector uptime | ~22.1h, pid 298870 | No restart |
| Published rows (shutdown) | 119,543,737 | From `shutdown_flush_done` |
| Published files | 402,192 | Live parquet files at stop |
| Complete manifests | 264 | All `row_count_match=true` at creation |
| Manifest rows sum | 119,047,046 | Gap ≈ 496,691 vs published ≈ uncompacted live tail + open window |
| Local outputs present | 146 / 60,047,046 rows | `compacted/` + `sent/` only |
| Local missing outputs | 118 | **All 118 present on remote** (0 truly missing) |
| `row_delta_local` (tool) | **-59,000,000** | Blind to remote + `sent/` retention |
| Remote objects | 263 files / **5.615 GiB** | `rclone size backup1tb:spread-compacted` |
| Transfer sqlite | `sent=264` (incl. 1 old smoke not on this prefix) | `confirmed=0`; no `row_count` column |
| Sqlite↔remote size mismatches | 0 | Among overlapping filenames |
| Backlog at stop | 1 file / ~21.1 MB / 426,086 rows | `…T113000Z_…T113500Z` — **transferred shortly after** (remote now 264) |
| Post-stop compaction | new local backlog appeared | Timers still active; e.g. `…T113500Z_…T114000Z` (~19 MB) from remaining archive |
| Spool files | 0 | |
| Failed batches | 0 lines | |
| Backup summaries | 253 success / 0 fail | `transfer_watchdog_kills` sum **0** |
| Compaction alerts | **6873** all `FileNotFoundError` | Stale path after `sent/` retention (~01:53Z→11:37Z) |
| Disk free `/` | **5.6 GiB** (81% used) | Was ~20 GiB after mid-canary cleanup |
| Live footprint | ~13 GiB (archived ~12.0 GiB / 400240 files; active ~0.06 GiB) | |
| Compacted local | ~3.0 GiB (145 sent + 1 backlog) | |
| Compactor/backup timers | **still active** | Collector systemd inactive/disabled |
| ops_alerts | **crashed** | ISO timestamp in `compactor.log` not parseable as float |

### Backup sample verify (3 files, download SHA + rows)

| File | Local present | Rows = manifest | SHA = manifest | Notes |
|---|---|---|---|---|
| `spread_20260803T133500Z_…134000Z` | no (retention) | yes (1,000,000) | yes | Early window, remote-only |
| `spread_20260804T000000Z_…000500Z` | yes | yes (600,000) | yes | SHA match local |
| `spread_20260804T112500Z_…113000Z` | yes | yes (420,960) | yes | Recent window |

## 5. Candidate interpretations

1. **Healthy end-to-end canary cut short** — collector/compaction/transfer worked; remote holds durable objects; early stop is administrative.
2. **Lifecycle/accounting defect under retention** — after `sent/` erase, `canary_24h.py account` reports huge local delta, and compactor emits FNF `compaction_alert` storms for already-durable windows.
3. **Disk still the operational cliff** — free space fell from ~20 GiB post-cleanup back to ~5.6 GiB; another long full-volume soak without lean/retention tuning remains risky.

## 6. Why READY remains blocked

1. Wall-clock canary did not reach planned T+24h (early stop).
2. Pass criterion “no stale-path `FileNotFoundError` / `compaction_alert` storms after `sent/` moves” **failed** (6873 alerts).
3. Daily local accounting tool reports `row_delta_local != 0` once retention removes `sent/` copies (even though remote inventory is complete for those names).
4. Transfer schema still has `sent` only (no `confirmed` / `row_count`), so remote row accounting is inventory + spot SHA, not first-class.
5. `ops_alerts.py` cannot currently finish a clean check against production `compactor.log` timestamps.
6. One compacted backlog file was still local at stop (timers should clear it; not treated as data loss).

## 7. Success criteria mapping

| Criterion | Result |
|---|---|
| ~24h uptime without forced kill | **partial** (~22.1h; operator SIGINT) |
| Expected shutdown / drain | **pass** (SIGINT, flush done, no escalate) |
| ops_alerts clean | **fail / tool broken** |
| Local `row_delta_local == 0` | **fail as reported** (retention artifact) |
| Remote holds sent compacted files | **pass** (263/263 canary windows on remote; sample SHA OK) |
| No FNF / compaction_alert storms | **fail** |
| Disk did not force silent loss | **pass** (tight but no collector death / no failed batches) |

## 8. VPS / storage validation notes (post-stop)

Safe read-only / status updates performed:

- SIGINT drain; status → `early_stopped`
- `canary_24h.py --action status` / `--action account --remote-path spread-compacted`
- `rclone size/lsl backup1tb:spread-compacted`
- Transfer sqlite summary; 3-file remote download SHA/row check
- `df -h /`; backlog/sent/live counts; log tails

Not done (per constraints): lean flags, collector restart, systemd enable, dataset deletes.

Compactor + backup-transfer timers left running (allowed).

## 9. Recommended next step

1. **Do not claim READY / do not enable `spread-collector.service` yet.**
2. Fix or gate the **post-retention compaction revalidation** so durable/`sent`-then-deleted windows do not spam `compaction_alert` (this was a prior soak blocker that returned under 12h sent retention).
3. Teach accounting / alerts to treat **remote `spread-compacted` + sqlite `sent`** as durable when local `sent/` is retained away.
4. Fix `ops_alerts.py` timestamp parsing for ISO compactor events.
5. Then run a **lean soak** (disk-aware: lean writer flags and/or tighter archive retention / smaller live footprint) before another 24h claim — disk returned to ~5.6 GiB free and is the practical limiter.

Optional immediate: confirm the 1-file backlog transfers on the next `spread-backup-transfer.timer` tick.
