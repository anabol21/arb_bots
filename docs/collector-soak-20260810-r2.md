# Collector re-soak (r2) — 2026-08-10

> **Host migration (2026-08-10):** current production collector host is `root@38.180.94.108`. The IP below is the historical host for this report.

Вердикт: **NO-GO** для полного контура `collector + compact (+ backup)` на 2 GiB VPS при оставшемся thick Aug 8+ live backlog.

Предшествующий drain (collector OFF) закрыл **Gate A**. Re-soak smoke 30 м: ingest lean+bars снова зелёный; во время collector-on — **7×** compactor `oom-kill`, **0** `compaction_complete`. 2 ч soak и controlled restart **не запускались**.

Связанный первый прогон: [`docs/collector-soak-20260810.md`](collector-soak-20260810.md) (NO-GO #1).

---

## 1. Pipeline block

```text
Phase A (historical drain, collector OFF)
  → controlled compact --max-windows 1 + tick backup
  → bars timer STOPPED (RAM)

Phase B (re-soak)
  collector lean+bars → /data/live + /data/bars
  → compactor (MemoryMax=1200M, --max-windows 1)
  → tick backup_transfer → backup1tb:spread-compacted
  → bars backup DELAYED (off during smoke)
```

| Контекст | Значение |
|----------|----------|
| Код | локальный репо → VPS `/root/spread_staging` |
| Исполнение | VPS `root@38.244.198.42` |
| Runtime log | `/var/log/spread/runtime.log` |
| Compactor log | `/var/log/spread/compactor.log` |
| Первая материализация | `/data/live`, `/data/bars` |
| Durable (ticks) | `backup1tb:spread-compacted` |
| Evidence | `/tmp/collector-resoake-drain.txt`, `/tmp/collector-resoake-t0.txt`, `/tmp/collector-resoake-evidence.log`, `/tmp/collector-resoake-final.txt` |

---

## 2. Phase A — Drain / Gate A

### Setup

- Collector **stopped** (systemd inactive).
- Rogue non-systemd `screaner_b_o.py` + `ping_okx_bybit_2h` (started ~17:19, ~75 MiB) **killed** ~18:04 to keep drain clean.
- `spread-bars-backup-transfer.timer` **stopped** for entire drain + smoke (RAM headroom; Conflicts= already in units).
- Compactor timer stopped during controlled oneshots; tick backup timer kept active.

### Baseline → after 12 oneshots

| Metric | Baseline (~17:11Z) | After drain loop (~18:36Z) |
|--------|--------------------|----------------------------|
| Live parquet (excl. archived) | **622731** | **611307** (−~11k) |
| Live excl. archived | ~10.2 GiB (`du`) | ~9.4–10.1 GiB class (still huge) |
| Remote Aug 8 | **97** | **110** (→ **112** by post-smoke) |
| Remote Aug 9 / 10 | **0 / 0** | **0 / 0** |
| Controlled oneshots | — | **12 success / 0 OOM / 0 fail** |
| Last window compacted | — | `spread_20260808T090000Z_…` (1008 sources) |

`du -sb /data/live` остаётся ~flat потому что archive живёт под тем же деревом; прогресс смотреть по parquet count + remote Aug 8 + `compaction_complete` в `compactor.log`.

### Gate A

**PASS.** Несколько подряд (`12`) `--max-windows 1` oneshot exit 0, **0** OOM при collector OFF; backlog visibly reduced vs smoke-era counts; remote Aug 8 рос.

Residual: каждое thick окно всё ещё ~672–1008 source files, peak RSS ~0.9–1.06 GiB **без** collector → dual-load на 1.9 GiB host остаётся рискованным. Code harden (Gate B) **не** применялся — Gate A достигнут раньше.

---

## 3. Phase B — Re-soak

### Preflight (PASS)

| Gate | Result |
|------|--------|
| Disk free | **7.8 GiB** (≥7) |
| Units | MemoryMax=1200M, `--max-windows 1`, Conflicts= OK |
| `reset-failed` | Done; collector NRestarts=0 |
| Bars timer | **left OFF** for smoke (documented) |

T0: `/tmp/collector-resoake-t0.txt` (~18:40Z). Live parquet **611307**, remote Aug 8 **110**.

### Start order

1. `spread-compactor.timer` + `spread-backup-transfer.timer` (~18:41)
2. Idle compact cycles completed **0 OOM** before collector (windows through ~09:05Z Aug 8 class)
3. `spread-collector.service` started **18:58:42Z** — smoke T0 **18:58:50Z**
4. Bars timer **not** started

### Smoke 30 м (FAIL) — T0 18:58:50Z → ~19:29Z

| Check | Pass? | Evidence |
|-------|-------|----------|
| Collector active, NRestarts stable | **PASS** | active; NRestarts=**0** |
| lean + bars + pairs≈337 | **PASS** | `schema_mode=lean`, `collect_bars=true`, pairs=337 |
| Ingest progress | **PASS** | published_rows → **~2.6M**, bar_published_rows → **2000**, failures=0 |
| ≥1 `compaction_complete` while collector ON | **FAIL** | **0** since smoke T0 |
| Compactor 0 oom-kill in smoke window | **FAIL** | **7×** `Failed with result 'oom-kill'` 19:01–19:29 UTC |
| `archive_retention_complete` in window | **FAIL** | 0 |
| ops_alerts --once | **FAIL** | `oom_or_restart_signal` (compactor_journal≈6 in lookback); lag / missing-cycle warnings |

Dual-load sample (~19:26): collector RSS ≈525 MiB, compactor RSS ≈768–1012 MiB, MemAvailable often &lt;300 MiB.

Smoke FAIL → `systemctl stop spread-collector` at **19:32:17Z**. Core 2 h + restart **skipped**.

---

## 4. Historical drain vs soak continuity

| Track | Finding |
|-------|---------|
| **Historical drain (collector OFF)** | Gate A PASS; Aug 8 windows advanced (~08:00→~09:05 during controlled loop + idle); remote Aug 8 **97→112**; **0** OOM in controlled drain |
| **Soak continuity (collector ON)** | Ingest OK; compact **не** дал complete; **7×** OOM — критерий успеха r2 **не** выполнен |
| Split | Рост remote Aug 8 во время drain **не** доказывает dual-load contour |

---

## 5. Success criteria → verdict

| Вердикт | Условие | Итог |
|---------|---------|------|
| GO | 0 OOM; ≥1 complete under collector; disk stable; restart clean | **не выполнено** |
| GO WITH CONDITIONS | contour alive with lag only | **не применимо** — OOM storm |
| **NO-GO** | OOM / 0 complete under collector | **7× OOM + 0 complete** |

**NO-GO** (r2): drain mitigates collector-off compaction, but **does not** make thick-window compact fit beside full-universe lean+bars on this host.

Partial positives: Gate A drain stability; ingest health; bars-off ops choice avoided bars/rclone RAM fight (Conflicts= already present).

---

## 6. Final runtime state

| Unit | State |
|------|-------|
| `spread-collector.service` | **stopped** |
| `spread-compactor.timer` | **active** (continue historical drain) |
| `spread-backup-transfer.timer` | **active** |
| `spread-bars-backup-transfer.timer` | **inactive** (left off after r2) |

Disk free ≈ **7.6 GiB**. Collector NRestarts **0**.

---

## 7. Recommended next step

1. Continue collector-OFF drain until remaining windows are thin enough that peak compact RSS + collector RSS ≪ 1.9 GiB **or** raise host memory / serialize compact into collector-off slots only.
2. Optional Gate B: further RSS cut in `compactor.py` / lower systemd Memory pressure experiments with `/usr/bin/time -v` on the python process (not `systemctl`).
3. Re-soak success metric unchanged: ≥1 `compaction_complete` **during** collector-on **and** 0 compactor oom-kill in that window.

---

## Key numbers (quick)

| Item | Value |
|------|-------|
| Gate A consecutive success / OOM | **12 / 0** |
| Drain live parquet | 622731 → 611307 |
| Remote Aug 8 (drain→post) | 97 → 112 |
| Smoke compaction_complete (collector ON) | **0** |
| Smoke compactor OOM | **7** |
| Smoke published_rows / bar rows | ~2.6M / 2000 |
| Bars timer during smoke | **OFF** |
