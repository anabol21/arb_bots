# Compactor fix validation — 2026-08-10

> **Host migration (2026-08-10):** current production collector host is `root@38.180.94.108`. The IP below is the historical host for this report.

**Track:** 1 — collection / storage reliability  
**Stage:** compaction → archive retention → backup transfer  
**Environments:** local (unit tests) · VPS runtime `root@38.244.198.42` · durable remote `backup1tb:spread-compacted` / `backup1tb:spread-bars`  
**Ownership:** Runtime Storage (implement) · Validation (this evidence)

## Verdict

**GO WITH CONDITIONS** for unattended operation.

Hardened streaming compaction completes thick windows without host OOM storm, disk was reclaimed to ~7.2 GiB free, systemd guards and alerts are deployed. Residual conditions: peak RSS still ~1.05 GiB on 1k-file lean windows (needs `MemoryMax=1200M` + `--max-windows 1`), Aug 8–10 live still draining to remote, bars hive backlog large.

## Pipeline block

| Item | Value |
|------|-------|
| Failure mode addressed | Compactor OOM (~1 GiB+) → no `archive_retention_complete` → ENOSPC |
| Frozen areas | WS ingest / parse / spread / trading untouched |
| Staging code | `/root/spread_staging` |

## Changes shipped (local → VPS staging)

1. **`app/storage/compactor.py`**
   - `ParquetFile.iter_batches` (no full `read()`)
   - `gc` + Arrow `release_unused` after sources/windows
   - `--max-windows` / `--iter-batch-rows`
   - Skip discover when window budget exhausted
   - Two-pass discover when `max_windows` set (do not retain path lists for later windows)
   - Claimed paths only from non-complete manifests
   - Per-manifest archive prune (no multi-million path set)
2. **`deploy/systemd/spread-compactor.service`** — `MemoryMax=1200M`, `OOMPolicy=stop`, `ARROW_DEFAULT_MEMORY_POOL=system`, `--max-windows 1`, `Conflicts=spread-bars-backup-transfer.service`
3. **`deploy/systemd/spread-bars-backup-transfer.service`** — mutual `Conflicts=`, `--skip-sha-verify-below-bytes 1048576`, `--max-files 500`, `MemoryMax=700M`
4. **`app/storage/backup_transfer.py`** — size-only verify under threshold; `transfer_enospc_alert`
5. **`validation/ops_alerts.py`** — compaction lag, missing complete/retention events, live growth with empty tick backlog, ENOSPC, journal OOM heuristic
6. **Docs** — runbook + forensics pointer

## Phase status

| Phase / todo | Status |
|--------------|--------|
| Phase 0 reclaim + salvage | **Done / in progress drain** — journal vacuum earlier; emergency archived reclaim freed **~2.98 GiB**; salvage compact+backup loop running (`--max-windows 1`) |
| Phase 1 streaming harden | **Done** — tests green; redeployed |
| Phase 1C–D systemd/alerts | **Done** — units + `ops_alerts.py` deployed |
| Phase 2 bars | **In progress** — skip-SHA drain running; backlog still large (~286k files at start) |
| Phase 3 validation | **Partial** — V1–V2 done; V3 soak short/conditional; V4–V6 notes below |

## Evidence

### V1 — local

```text
python3 -m py_compile app/storage/compactor.py
python3 -m unittest tests.test_compactor -v
→ Ran 17 tests … OK
```

### V2 — VPS thick-window RSS

| Run | Peak RSS | Notes |
|-----|----------|-------|
| Pre-harden salvage (partial streaming + full discover) | **1364680 KiB (~1.30 GiB)** | Window complete; discover of ~620k live paths spiked RSS after write |
| After discover skip / system pool (still giant retention set) | **1358216 KiB (~1.30 GiB)** | Same class |
| After claimed/retention/discover harden | **1098216 KiB (~1.05 GiB)** | `spread_20260808T072000Z_…` 1008 sources / 300k rows; exit 0; `row_count_match=true` |
| Salvage loop iters 1–2 | **1090400 / 1088088 KiB (~1.04 GiB)** | Stable; mid-write RSS observed ~832 MiB |

Target “clearly below ~1 GiB” **not fully met**; mitigated with `MemoryMax=1200M` + `--max-windows 1` + bars `Conflicts=`.

### Phase 0 disk / reclaim

| Metric | Before harden drain | After emergency reclaim + early salvage |
|--------|---------------------|-----------------------------------------|
| `df /` avail | ~3.8 GiB (87%) | **~7.8 GiB (73%)** |
| Archived | ~3.5 GiB | **~126 MiB** (then growing as new windows archive) |
| Live (incl. remaining) | ~15 GiB | **~11 GiB → draining** |
| Compacted pending | ~1.9 GiB / ~170 files | draining; Aug 7–8 local pending + remote Aug 7 growing |

`validation/emergency_reclaim_archived.py --execute`:  
`removed≈196560` paths, `bytes≈2.98 GiB` (only manifests with durable compacted/sent/offloaded).

### Remote continuity (ticks)

`backup1tb:spread-compacted` at validation time:

| Date (UTC window prefix) | Remote object count (lsf) |
|--------------------------|---------------------------|
| 20260803–06 | present (historical) |
| **20260807** | **253+** (growing during drain) |
| **20260808–10** | **not yet on remote** (local compacted has Aug 7–8; Aug 8–10 live still being compacted) |

Remote size sample: `count≈942`, `bytes≈12.4 GiB`. Continuity for Aug 8–10 is **salvage-in-progress**, not lost-by-delete (live still on VPS).

### V3 — soak

Collector + timers were **stopped** during emergency salvage. Restart decision: only after free disk ≳7 GiB and MemoryMax unit reload (see ops section). Long 2–4 h unattended soak may be incomplete in this session; recommend short post-restart observation then timer enable.

### V4 — many-files stress

Local: `test_streaming_write_handles_many_small_sources` (40 files, asserts no `ParquetFile.read`).  
VPS: production windows with **1008** source files compacted successfully.

### V5 — disk invariant

Free ≥ ~7 GiB after reclaim; do not reclaim archived without remote/sent/offloaded proof.

### V6 — collector restart

**Not restarted in this session** while 8-window salvage loop + bars drain still run (avoids RAM contention with ~1.05 GiB compact peaks). Disk already meets ~8 GiB free target (~7.8 GiB). Restart when `/tmp/salvage-loop.log` shows `SALVAGE LOOP DONE`, then:

```bash
systemctl start spread-collector.service
systemctl start spread-compactor.timer spread-backup-transfer.timer spread-bars-backup-transfer.timer
```

## Ops follow-up (same day)

```bash
# status
df -h /; free -h
pgrep -af 'compactor|backup_transfer|salvage'
tail -n 50 /tmp/salvage-loop.log

# when salvage loop idle and free≳7G:
systemctl start spread-collector.service
systemctl start spread-compactor.timer spread-backup-transfer.timer spread-bars-backup-transfer.timer
/root/venv/bin/python validation/ops_alerts.py --once
```

## Success criteria checklist

| Criterion | Result |
|-----------|--------|
| Streaming write (no full-table concat) | Pass |
| Unit tests green | Pass |
| Peak RSS clearly <1 GiB | **Fail soft** (~1.05 GiB) → MemoryMax 1200M |
| Disk ≥ ~8 GiB free | **~7.8 GiB** (meets intent) |
| Aug 7–10 remote continuous | **Partial** — Aug 7 on remote (growing); 8–10 draining |
| Alerts for lag / retention silence | Deployed |
| Collector + timers restarted | **No** — deferred until salvage loop idle |
| Unattended GO | **GO WITH CONDITIONS** |

## Recommended next step

1. Let salvage loop + tick/bars backup finish Aug 8–10 live → remote.  
2. Restart collector + timers; 2–4 h soak with `ops_alerts.py` and zero OOM in journal.  
3. Optional: further RSS cut (stream manifests / avoid loading all source JSON into RAM).
