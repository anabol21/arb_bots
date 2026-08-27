# Compactor state retention (12h) — 2026-08-25

**Track:** 1 — collection / storage (D)  
**Stage:** compaction → archive retention → backup transfer  
**Host:** VPS `root@38.180.94.108`  
**Ownership:** Runtime Storage (patch) · Validation (24h gate)  
**Git:** суточный gate **Pass** (пользователь 2026-08-27 + collector `active` PID `902378` с 20.08, `NRestarts=0`). Коммит трека D — актуальная working top-версия сборщика.

## 24h validation — Pass (2026-08-27)

Observation: deploy **11:08 UTC 25 Aug** → check **~10:17 UTC 27 Aug** (>24h).

| Check | Result |
|-------|--------|
| Collector | `active`, MainPID **902378**, **NRestarts=0** (same process since 20.08 13:08 UTC) |
| Compactor after fix | oneshot 25.08 success; prune + `compaction_complete` resumed; no return to pre-fix “every ~2 min oom-kill while lag grows” as the steady state |
| User soak | green over >1 day (explicit) |
| Intent | commit D-track as current production collector baseline |

Re-run commands in the plan section if auditing later; SSH may time out on heavy `find` — prefer `ls …/spread_*.json \| wc -l` and heartbeats.

## Problem (plain)

`sent/` parquet after backup already aged out (~12h). Compactor **window manifests** in `/data/compacted/.state/spread_*.json` did **not**. Thousands of JSON files kept full source-path lists forever. Each oneshot loaded all of them into RAM (~4.5 GiB RSS) and died on unit `MemoryMax=2500M` (`CONSTRAINT_MEMCG`), so:

- no further `compaction_complete` after `2026-08-25T01:15:18Z` (`…T224000Z_…T224500Z`);
- archive retention stalled; `/data/live` grew (~10 GB/day);
- tick backup itself was healthy (`compacted` pending=0, `backlog_files_count=0`).

Disk free (~18G) was unrelated to the kill: OOM was cgroup RAM, not ENOSPC.

## Fix (code)

File: `app/storage/compactor.py`

- Before `_load_manifests`, stream `_prune_expired_complete_states` **one JSON at a time**:
  1. only `status=complete`;
  2. `window_end` older than `--retention-hours` (prod unit: **12**);
  3. never while output still sits in `/data/compacted/` (pending transfer);
  4. prune matching `/data/live/archived/` sources first;
  5. delete the JSON only when archives for that manifest are gone.
- Events: `compaction_state_pruned`, `compaction_state_prune_complete`.
- Does **not** touch `backup_manifest.sqlite3`.
- Long-term audit: `compactor.log` + remote `backup1tb:spread-compacted`.

Tests: `tests/test_compactor.py` (expired prune / keep pending / keep young).

Docs: this file; one-line note in `docs/overnight-prod-canary-design-20260811.md`; runbook path table below.

## Deploy (2026-08-25 ~11:05–11:08 UTC)

- Copied `compactor.py` → `/root/spread_staging/app/storage/compactor.py`.
- **Collector not restarted** (PID `902378`, `NRestarts=0`).
- One oneshot: `systemctl start spread-compactor.service` → `Result=success`.

Observed:

| Metric | Before | After oneshot |
|--------|--------|----------------|
| `spread_*.json` in `.state` | ~3825 | **33** |
| `pruned_manifests` | — | **3793** |
| `removed_archive_files` (stream prune) | — | **211797** |
| Next compact | stalled | `…T224500Z_…T225000Z` `row_count_match=true` |
| `df` free on `/` | ~18G | ~22G |
| `/data/live` | ~8.9G | ~5.3G |

`deferred_archives_remain=32` — manifests still inside retention / archives not yet eligible; expected to clear on later cycles.

## Data integrity after OOM storm

- Completed compacted windows already on remote were not rewritten by OOM.
- Failure mode was **lag** (uncompacted live batches), not silent corruption of `complete` outputs.
- Mid-write failure leaves `.inprogress` / non-complete state; restart path rebuilds from sources.
- No mass `compaction_alert` checksum failures observed on the recovery window.

## 24h validation — Pass (2026-08-27)

Observation: deploy **11:08 UTC 25 Aug** → check **~10:17 UTC 27 Aug** (>24h).

| Check | Result |
|-------|--------|
| Collector | `active`, MainPID **902378**, **NRestarts=0** (same process since 20.08 13:08 UTC) |
| User soak | green over >1 day (explicit) |
| Intent | D-track committed as current production collector baseline |

## 24h validation plan (re-audit commands)

Observation window reference: from deploy **11:08 UTC 25 Aug**.

Read-only checks on VPS:

```bash
# Collector still the same process family / no surprise restarts
systemctl show spread-collector.service -p ActiveState,MainPID,NRestarts --no-pager
grep 'heartbeat |' /var/log/spread/runtime.log | tail -n 3

# Compactor: no new oom-kill storm; prune + complete continue
journalctl -u spread-compactor.service --since '24 hours ago' --no-pager \
  | grep -c 'oom-kill' || true
grep -aE 'compaction_state_prune_complete|compaction_complete|archive_retention_complete' \
  /var/log/spread/compactor.log | tail -n 40

# State / archive bounded
ls /data/compacted/.state/spread_*.json | wc -l   # expect O(hours/5min), not thousands
find /data/live/archived -type f -name '*.parquet' | wc -l
du -sh /data/live /data/live/archived /data/compacted/.state
df -h /

# Backup still empty backlog
tail -n 5 /var/log/spread/backup-transfer.log
```

**Pass (GO for git as current top working):**

1. Collector `active`, `NRestarts` unchanged or explained; `failures=0` on heartbeats.
2. Compactor: ongoing `compaction_complete` (lag shrinking toward live edge); **no** recurring `result=oom-kill` every ~2 min.
3. `spread_*.json` count stays on the order of **≤ ~200** (12h × 12 windows/h, plus small slack), not thousands.
4. Disk free not cliffing; `/data/live` not re-growing like the pre-fix ~10 GB/day stall.
5. Backup summaries still `backlog_files_count=0` (or transient few that drain).

**Fail / hold push:** renewed memcg OOM storm; state JSON climbing again; ENOSPC; collector instability tied to storage.

## Git gate (after Pass)

When Validation signs the 24h check:

1. Commit only the intentional set (suggested):
   - `app/storage/compactor.py`
   - `tests/test_compactor.py`
   - `docs/compactor-state-retention-20260825.md`
   - `docs/compaction-backup-runbook.md` (state retention note)
   - `docs/overnight-prod-canary-design-20260811.md` (table line already updated)
2. Push as current working top of track-1 storage (no force to main without explicit ask).
3. Do **not** bundle unrelated dirty tree from other tracks.

## Non-goals

- Raising `MemoryMax` as the permanent fix (optional headroom only).
- Deleting `backup_manifest.sqlite3` rows with the JSON prune.
- Bars tree / B-bot paths.
- Stopping the collector for this change.
