# Collector soak — 16 GiB / 80 GiB host — 2026-08-10

Вердикт: **GO WITH CONDITIONS** для контура `collector lean+bars + compact + tick/bars backup` на новом VPS.

Главный failure mode старого 2 GiB хоста (compactor OOM при collector-on) **снят**. Smoke и ≥2 h soak: **0** oom-kill, **≥1** `compaction_complete` при работающем collector. Условие: после роста bars backlog таймеры `Conflicts=` взаимно гасят bars↔compactor (`SIGTERM`), из‑за чего lag compact растёт — нужна более редкая/длинная bars schedule или сериализация без thrash.

База сравнения: [`docs/collector-soak-20260810-r2.md`](collector-soak-20260810-r2.md) (NO-GO на `root@38.244.198.42`).

---

## 1. Pipeline block

```text
fresh /data (empty) → collector lean+bars → /data/live + /data/bars
  → compactor (MemoryMax=2500M, --max-windows 1)
  → tick backup → backup1tb:spread-compacted
  → bars backup → backup1tb:spread-bars  (ON на 16G; задокументировано)
```

| Контекст | Значение |
|----------|----------|
| Код | локальный репо → VPS `/root/spread_staging` (greenfield deploy) |
| Исполнение | VPS `root@38.180.94.108` (hostname `a845945761.local`) |
| Runtime log | `/var/log/spread/runtime.log` |
| Compactor log | `/var/log/spread/compactor.log` |
| Первая материализация | `/data/live`, `/data/bars` (**fresh empty**, не миграция backlog) |
| Durable (ticks) | `backup1tb:spread-compacted` |
| Durable (bars) | `backup1tb:spread-bars` |
| Evidence | `/tmp/collector-soak-16g-*.txt`, `/tmp/collector-soak-16g-*-evidence.log` |

---

## 2. Host readiness (greenfield)

| Item | State |
|------|-------|
| RAM / disk | **15 GiB** available class / **79G** SSD (~**71 GiB** free at T0) |
| `/root/spread_staging` | Deployed (`app/`, `validation/`, `deploy/`, universe CSV) |
| `/root/venv` | Created; pyarrow/pandas/websockets/ccxt OK |
| `/data/{live,bars,spool,compacted}` | Created empty |
| systemd units | Installed + enabled from `deploy/systemd/` |
| Compactor MemoryMax | **2500M** (было 1200M); `--max-windows 1` сохранён |
| Bars MemoryMax | **1500M** (было 700M); `Conflicts=compactor` сохранён |
| Env | `SPREAD_LEAN_SCHEMA=1`, `SPREAD_COLLECT_BARS=1` |
| rclone | Copied from old host (`backup1tb` lists OK); key `id_ed25519_uploader` |
| Rogue | Stopped leftover hostcap experiment (`/root/spread_venv`, N1 XRP) before soak |

---

## 3. Preflight T0

`/tmp/collector-soak-16g-t0.txt` — **2026-08-10T20:39:36Z**

| Gate | Result |
|------|--------|
| Disk free | **~71 GiB** (≥7) |
| Units MemoryMax / max-windows / Conflicts | OK |
| Idle compact oneshot | exit 0, 0 OOM |
| Data state | **fresh_empty** (0 live/bars/compacted files) |

Start order:

1. `spread-compactor.timer` + `spread-backup-transfer.timer` + idle cycle  
2. `spread-collector.service` @ **20:41:42Z** (smoke T0 **20:41:50Z**)  
3. `spread-bars-backup-transfer.timer` **ON** (выбор для 16G)

---

## 4. Smoke ~30 м — PASS

T0 **20:41:50Z** → ~**21:11:50Z**. Evidence: `/tmp/collector-soak-16g-smoke-evidence.log`, eval `/tmp/collector-soak-16g-smoke-eval.txt`.

| Check | Pass? | Evidence |
|-------|-------|----------|
| Collector active, NRestarts stable | **PASS** | active; NRestarts=**0** |
| lean + bars + pairs≈337 | **PASS** | `schema_mode=lean`, `collect_bars=true`, pairs=337 |
| Ingest progress | **PASS** | published_rows → **~1.9–2.0M**, bar_published_rows → **2000**, failures=0 |
| ≥1 `compaction_complete` while collector ON | **PASS** | **6** completes in smoke window |
| Compactor 0 oom-kill | **PASS** | **0** |
| ops_alerts --once | **PASS** | `ops_alert_ok` at smoke end |
| Tick remote | **PASS** | remote `20260810` → **5**; sent=5 |

Collector RSS ≈560 MiB; MemAvailable ≈**14.5 GiB** throughout smoke.

---

## 5. Core soak ≥2 h + controlled restart — PASS (with lag condition)

Soak T0 **21:14:12Z** → final **23:14:12Z**. Sampler `/tmp/collector-soak-16g-soak-evidence.log` (6 samples @20 m).

| Metric | Value |
|--------|-------|
| Duration | **2 h** |
| Compaction completes (smoke+soak to 22:00) | **16** under collector-on before stall |
| Completes during soak window | **10** (21:14–22:00) + **1** post-manual oneshot 23:17 |
| Compactor OOM | **0** |
| Peak published_rows (pre-restart) | **8 100 000** |
| Peak bar_published_rows (pre-restart) | **8500** |
| Bars coins | **336** |
| Remote ticks `20260810` | **16** (sent=16) |
| MemAvailable | **~14.4–14.5 GiB** steady |
| Disk free end | **~70 GiB** |
| Controlled restart | **22:54:13Z** PID 5276→24505, active, NRestarts=**0**, ingest resumes |
| Post-restart published_rows (~23:17) | **1 400 000** (new process counters) |

### Condition: bars↔compactor Conflicts thrash

После ~22:00 bars hive transfer стал длиннее интервала таймера. `Conflicts=` гасит пару сервисов `SIGTERM` (не OOM):

| Signal | Count since smoke T0 |
|--------|----------------------|
| compactor `status=15/TERM` | **14** |
| bars-backup `status=15/TERM` | **28** |
| compactor `oom-kill` | **0** |

Итог: `compaction_lag_high` / `compaction_complete_missing` в `ops_alerts` на финале (~77 м lag). Ручной oneshot compact **23:17** снова дал `compaction_complete` (window `220000Z–220500Z`) — код/RAM OK, scheduling conflict.

---

## 6. Comparison vs old 2 GiB r2

| Metric | Old 2G / 30G (r2 NO-GO) | New 16G / 80G |
|--------|-------------------------|---------------|
| Host | `38.244.198.42` ~1.9 GiB / 30G | `38.180.94.108` **15 GiB** / **79G** |
| `/data` | thick Aug 8+ backlog | **fresh empty** |
| Disk free (smoke) | ~7.6–7.8 GiB | **~70–71 GiB** |
| MemAvailable under dual-load | often &lt;300 MiB | **~14.5 GiB** |
| Smoke compaction_complete (collector ON) | **0** | **6** |
| Smoke compactor OOM | **7** | **0** |
| Soak ≥2 h | skipped | **ran** |
| Controlled restart | skipped | **OK** |
| Peak published_rows | ~2.6M (smoke only) | **8.1M** pre-restart |
| Bars coins | 336 (timer OFF in r2 smoke) | **336** (timer **ON**) |
| NRestarts | 0 | **0** |
| Remote tick backup | drain-era only under collector-off | **16** files today under load |
| Verdict | **NO-GO** | **GO WITH CONDITIONS** |

---

## 7. Success criteria → verdict

| Вердикт | Условие | Итог |
|---------|---------|------|
| GO | 0 OOM; ≥1 complete under collector; disk stable; restart clean; no material ops lag | OOM/complete/restart **OK**, но bars/compact thrash → lag |
| **GO WITH CONDITIONS** | contour alive; primary RAM dual-load fixed; residual ops issue documented | **выбрано** |
| NO-GO | OOM / 0 complete under collector | не применимо |

**GO WITH CONDITIONS:** 16 GiB снимает dual-load OOM; полный контур с bars ON работает по RAM; остаётся таймерный конфликт bars↔compactor при длинном hive transfer.

---

## 8. Final runtime state

| Unit | State |
|------|-------|
| `spread-collector.service` | **active** (PID 24505 post-restart; NRestarts=0) |
| `spread-compactor.timer` | **active** |
| `spread-backup-transfer.timer` | **active** |
| `spread-bars-backup-transfer.timer` | **active** |

Disk free ≈ **70 GiB**. MemAvailable ≈ **14 GiB**.

---

## 9. Recommended next step

1. Удлинить `OnUnitActiveSec` bars timer (например 15–30 m) и/или поднять `--max-files` batch с явным `TimeoutStartSec`, чтобы bars цикл заканчивался до следующего Conflicts-start.  
2. Альтернатива: оставить bars OFF в daytime drain slots; tick compact priority.  
3. Не откатывать MemoryMax=2500M / `--max-windows 1` без новой evidence.  
4. Опционально: 24 h canary на этом хосте после правки bars schedule.

---

## Key numbers (quick)

| Item | Value |
|------|-------|
| Smoke completes / OOM | **6 / 0** |
| Total completes under collector (to postdiag) | **17** |
| Compactor TERM / bars TERM | **14 / 28** |
| Peak published_rows / bar rows | **8.1M / 8500** |
| Remote Aug 10 ticks | **16** |
| Bars coins | **336** |
| Restart | clean @ 22:54Z |
| Verdict | **GO WITH CONDITIONS** |
