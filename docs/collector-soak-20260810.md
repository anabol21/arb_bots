# Collector soak experiment — 2026-08-10

> **Host migration (2026-08-10):** current production collector host is `root@38.180.94.108`. The IP below is the historical host for this report.

Вердикт: **NO-GO** для полного контура `collector + compact + backup` на текущем 2 GiB VPS при толстом historical live backlog.

Smoke 30 м: collector ingest lean+bars **зелёный**; compactor под нагрузкой collector — **OOM storm** (7× `oom-kill`). 2 ч soak и controlled restart **не запускались** (gate FAIL).

**Re-soak (r2):** after collector-OFF Gate A drain (12× compact success, 0 OOM), smoke was re-run — again **NO-GO** (7× OOM, 0 `compaction_complete` under collector). See [`docs/collector-soak-20260810-r2.md`](collector-soak-20260810-r2.md).

---

## 1. Pipeline block

```text
collector (lean+bars) → /data/live + /data/bars
  → compactor (--max-windows 1, MemoryMax=1200M)
  → tick backup_transfer → backup1tb:spread-compacted
  → bars backup_transfer → backup1tb:spread-bars
```

| Контекст | Значение |
|----------|----------|
| Код | локальный репо → VPS `/root/spread_staging` |
| Исполнение | VPS `root@38.244.198.42` |
| Runtime log | `/var/log/spread/runtime.log` |
| Первая материализация | `/data/live`, `/data/bars/bar_5m` |
| Durable (ticks) | `backup1tb:spread-compacted` (после transfer) |
| Durable (bars) | `backup1tb:spread-bars` |
| Evidence | `/tmp/collector-soak-t0.txt`, `/tmp/collector-soak-evidence.log` |

---

## 2. Existing files / modules

- Entrypoint: `app/screaner_b_o.py` via `spread-collector.service`
- Compactor / backup: `app/storage/compactor`, `app/storage/backup_transfer`
- Units: `deploy/systemd/spread-*.{service,timer}` (на VPS совпали с репо: MemoryMax=1200M, `--max-windows 1`, Conflicts bars↔compact, bars `skip-sha` / `max-files 500`)
- Gate: `validation/ops_alerts.py --once`

---

## 3. Candidate interpretations

1. **Collector OK, contour not** — full-universe lean+bars пишет; совместный RSS с thick compact window (~1k source files / ~1 GiB anon) не помещается рядом с collector на 1.9 GiB host.
2. **Historical drain vs soak continuity** — remote Aug 8 рос *до* / *в начале* окна (salvage + early compact); после старта collector compact перестал давать `compaction_complete` и начал OOM.
3. **False-positive alerts mixed with real** — `compaction_lag_high` на Aug 8 backlog ожидаем при `--max-windows 1`; `oom_or_restart_signal` и journal `oom-kill` — реальный блокер.

---

## 4. Key risks / failure modes (observed)

| Mode | Evidence |
|------|----------|
| Compactor OOM under collector | 7× `Failed with result 'oom-kill'` 16:31–17:00 UTC; kernel killed python ~900–1050 MiB RSS in `spread-compactor.service` cgroup |
| No compact progress in smoke | Last `compaction_complete` / `archive_retention_complete` at **16:22 / 16:23**; smoke T0 **16:27:39** → 0 completes in 30 м |
| Disk pressure mild | free 8.3 → 8.0 GiB over smoke (not ENOSPC) |
| Live still ~10 GiB historical | T0 live_bytes≈10.36 GiB; post-smoke ≈10.58 GiB — soak continuity of *new* windows not the only consumer |

Frozen WS/parse/spread/trading **not** modified. Destructive deletes of live Aug 8–10 **not** performed.

---

## 5. Experiment execution

### Preflight (PASS → started)

| Gate | Result |
|------|--------|
| Salvage | Loop **not** DONE; stopped manually at 16:07:38 UTC (`SALVAGE_STOPPED_MARK`); freed RAM ~1.4 GiB avail |
| Disk | **8.23 GiB** free (≥7, ≥8 prefer) |
| Units | MemoryMax=1200M, `--max-windows 1`, Conflicts, bars skip-sha/max-files **OK** |
| `reset-failed` | Done; NRestarts cleared to 0 |

### T0 snapshot (2026-08-10T16:07:57Z)

| Metric | Value |
|--------|-------|
| `df -h /` | 30G total, 20G used, **8.3G** avail (71%) |
| live parquet count | **618363** |
| live bytes | **10357734054** (~9.65 GiB) |
| bars coin dirs | **336** |
| bars bytes | **1075045736** |
| compacted pending / sent | **50 / 20** |
| remote ticks Aug 7/8/9/10 | **288 / 44 / 0 / 0** (total parquet listed **1032**) |
| collector | failed → reset → inactive |
| timers | enabled, inactive (stopped before experiment) |
| last compact complete | 15:53:27Z (`spread_20260808T074500Z_…`) |
| last retention complete | 15:54:04Z |

### Start order

1. Enabled/started `spread-compactor.timer` + `spread-backup-transfer.timer` (~16:09)
2. First compact cycles produced completes at 16:15 and 16:22 (**0 OOM** before collector)
3. Started `spread-collector.service` **16:26:32Z** — `schema_mode=lean`, `collect_bars=true`, `pairs=337`
4. Enabled/started `spread-bars-backup-transfer.timer` **16:27:39Z** (smoke T0)

### Smoke 30 м (FAIL)

| Check | Pass? | Evidence |
|-------|-------|----------|
| Collector active, NRestarts stable | **PASS** | active; NRestarts=**0**; no collector OOM |
| lean + bars + pairs≈337 | **PASS** | heartbeats; pairs=337 |
| New live batches multi-coin | **PASS** | newer_files=**11724**, distinct_coins=**336** |
| Bars new batches sample coins | **PASS** | BTC/ETH/SOL/DOGE=4 each; newer_coins=**336**, files=**1329** |
| ≥1 compact+retention in window | **FAIL** | 0 since smoke T0 |
| Compactor no kill / within MemoryMax | **FAIL** | 7× oom-kill; peak RSS ≈1.0–1.05 GiB then global OOM |
| Tick backup progress | **PASS** | remote Aug 8 **44→96**; pending **0**; `transfer_success=true` |
| ops_alerts --once | **FAIL** | `oom_or_restart_signal` (compactor_journal=6); also lag/missing-cycle warnings |

Heartbeats (session after 16:26 start): `published_rows` → **3.4M**, `bar_published_rows` → **2000**, `failures=0`, spool=0.

Smoke FAIL → `systemctl stop spread-collector` at **17:02:28Z**.

### Core soak 2 h

**Skipped** (plan: FAIL smoke → do not enter 2 h).

### Controlled restart

**Skipped**.

---

## 6. Historical drain vs soak continuity

| Track | Finding |
|-------|---------|
| **Historical drain (Aug 7–8)** | Compactor+backup *without* full collector drained Aug 8 windows; remote Aug 8 **44→96** during experiment morning; Aug 9/10 still **0** on remote |
| **Soak continuity (new lean+bars)** | Ingest proven for **336** coins ticks+bars on `/data/*`; durable compact→remote for *new* soak windows **not** proven — compact OOM’d while collector ran |
| Split | Do **not** treat remote Aug 8 growth as proof that live soak windows survive the full contour under concurrent collector load |

---

## 7. VPS / storage validation plan (what this run covered)

Observed: runtime active, live/bars materialization, timer-driven compact/backup, journal OOM, rclone remote counts, ops_alerts.

Not claimed: 2–4 h soak, restart cleanliness under load, multi-day unattended READY, full remote bars catch-up, mount-remote primary durability (primary is local `/data`).

---

## 8. Success criteria → verdict

| Вердикт | Условие | Итог |
|---------|---------|------|
| GO | 0 OOM; compact+retention alive; remote soak growth; disk stable; bars≈live; restart clean | **не выполнено** |
| GO WITH CONDITIONS | contour alive with lag / free&lt;8 GiB but not falling | **не применимо** — OOM storm |
| **NO-GO** | OOM storm / ENOSPC / backlog=0 while live grows &gt;45 м / restart loop / bars only tail | **OOM storm + no compact in smoke** |

**NO-GO** for declaring the current script “works end-to-end” on this VPS with the present live backlog + full-universe collector.

Partial positives (do not override NO-GO):

- Collector lean+bars ingest is healthy in isolation (30 м, 336 coins, failures=0).
- Tick backup path works when compacted artifacts exist (Aug 8 remote +52 files).
- Units with MemoryMax / max-windows / Conflicts are deployed as intended — still insufficient vs collector+thick window.

---

## 9. Final runtime state (after experiment)

| Unit | State |
|------|-------|
| `spread-collector.service` | **stopped** (inactive) — NO-GO |
| `spread-compactor.timer` | **active** (historical drain may continue) |
| `spread-backup-transfer.timer` | **active** |
| `spread-bars-backup-transfer.timer` | **active** |

Disk free ≈ **8.0 GiB**. Compacted pending **0**. Bars coins **336**. Collector NRestarts **0** (clean stop).

---

## 10. Recommended next step

1. Keep collector **off** until compact can finish thick historical windows **without** OOM (or further reduce peak RSS / serialize more aggressively vs any other Python load).
2. Re-run soak only when: (a) live backlog bounded enough that one `--max-windows 1` window + collector fit in RAM, or (b) compact scheduled in collector-off windows, or (c) host memory increased.
3. Treat next experiment’s success metric as: ≥1 `compaction_complete` **during** collector-on smoke, with **0** compactor oom-kill in journal.

**Update:** r2 executed this plan (Gate A drain then re-soak). Gate A passed; dual-load smoke still NO-GO — details in [`collector-soak-20260810-r2.md`](collector-soak-20260810-r2.md).

---

## Key numbers (quick)

| Item | Value |
|------|-------|
| Disk T0 → smoke end | 8.23 → 7.92–8.0 GiB free |
| Compactor OOM (smoke window) | **7** |
| Remote Aug 8 T0 → post | **44 → 96** |
| Remote Aug 9 / Aug 10 | **0 / 0** |
| Bars coins | **336** |
| Collector NRestarts | **0** |
| Smoke published_rows / bar_published_rows | **3.4M / 2000** |
