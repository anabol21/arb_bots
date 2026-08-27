# Overnight prod canary — дизайн (2026-08-11)

> **Статус:** RUNNING — отдельный production canary запущен `2026-08-11T23:15:51Z`; плановый конец `2026-08-12T07:15:51Z`. Детали запуска: [`overnight-prod-canary-20260812-start.md`](overnight-prod-canary-20260812-start.md).  
> **Хост:** `root@38.180.94.108` (16 GiB RAM / 80 GiB SSD), staging `/root/spread_staging`.  
> **Старый хост:** `38.244.198.42` — retired для этого контура; thick backlog **не мигрируем**.  
> **База evidence:** [`collector-soak-20260810-16g.md`](collector-soak-20260810-16g.md) (GO WITH CONDITIONS), [`collector-soak-20260810-r2.md`](collector-soak-20260810-r2.md) (NO-GO dual-load OOM на 2 GiB), [`compaction-backup-runbook.md`](compaction-backup-runbook.md), [`canary-24h.md`](canary-24h.md).

---

## 1. Pipeline block

```text
fresh /data (empty или уже lean overnight-ready)
  → collector lean+bars, N≈337  (app/screaner_b_o.py)
  → /data/live/*.parquet  +  /data/bars/bar_5m/...
  → compactor (--max-windows 1) → /data/live/archived/ + /data/compacted/spread_*.parquet
  → tick backup_transfer → backup1tb:spread-compacted → local /data/compacted/sent/ → prune
  → bars backup_transfer (hive) → backup1tb:spread-bars → local /data/bars/sent/ → prune
```

| Контекст | Значение |
|----------|----------|
| Код | локальный репо → VPS `/root/spread_staging` |
| Исполнение | VPS `root@38.180.94.108` |
| Runtime log | `/var/log/spread/runtime.log` |
| Compactor log | `/var/log/spread/compactor.log` |
| Tick transfer log | `/var/log/spread/backup-transfer.log` |
| Bars transfer log | `/var/log/spread/bars-backup-transfer.log` |
| Первая материализация | `/data/live`, `/data/bars` (VPS-local `/data`) |
| Durable ticks | `backup1tb:spread-compacted` |
| Durable bars | `backup1tb:spread-bars` |
| Evidence overnight | `/tmp/overnight-prod-canary-20260812.log` (systemd supervisor `overnight-prod-canary-20260812.service`) |

---

## 2. Goal / non-goals

### Goal

За одну ночь (**~8–12 h**, wall-clock) доказать, что **текущий** скрипт (lean schema, N≈337, bars ON) на 16 GiB VPS способен **без присмотра** прогонять полный контур:

collect → write VPS → compact → backup → delete/move sent → folder transitions (ticks + bars),

с нулевым OOM и **без** bars↔compactor SIGTERM thrash как доминирующего failure mode.

**PASS overnight = unlock multi-day / weeks** с редкими status-check’ами; model track тянет данные с backup.

### Non-goals (явно OUT OF SCOPE)

| Исключено | Почему |
|-----------|--------|
| Latency / freshness / D–B branch эксперименты | Идут отдельным треком |
| Сравнение schema v1 vs lean по качеству сигналов | Schema уже lean в prod env |
| Миграция thick backlog со старого хоста | Greenfield fresh collection |
| Архитектурный rewrite writer/uploader | Только малый ops fix bars timer |
| Live trading / glue track 3 | Не открыт |
| Freeze unlock: WS ingest / parse / spread / trading | Не трогаем |
| Утверждение «profitability модели» | Model track отдельно |

---

## 3. Path table (lifecycle)

### Ticks

| Стадия | Путь | Ожидание overnight |
|--------|------|--------------------|
| Live final | `/data/live/*.parquet` | растёт, окна уходят в compact |
| Live tmp | `/data/live/.tmp/*.parquet.tmp` | эфемерно, не input compact |
| Archived sources | `/data/live/archived/` | после `row_count_match`; prune по retention (~12 h unit) |
| Compacted | `/data/compacted/spread_*.parquet` | backlog обычно 0–few между тиками |
| Compact state | `/data/compacted/.state/` | window manifests kept ≤ retention (~12 h unit); `backup_manifest.sqlite3` separate |
| Sent (confirmed local) | `/data/compacted/sent/` | после remote size+SHA; prune ~12 h |
| Durable remote | `backup1tb:spread-compacted/spread_*.parquet` | монотонный рост объектов за ночь |

### Bars

| Стадия | Путь | Ожидание overnight |
|--------|------|--------------------|
| Live hive | `/data/bars/bar_5m/base_coin=*/event_date=*/batch_*.parquet` | ~336 монет |
| Bars sent | `/data/bars/sent/...` | после confirm; prune ~12 h |
| Bars state | `/data/bars/.state/backup_manifest.sqlite3` | |
| Durable remote | `backup1tb:spread-bars/bar_5m/...` | рост hive за ночь |
| Compaction bars | — | **нет** (bars не компактятся) |

### Spool / quarantine

| Путь | Роль |
|------|------|
| `/data/spool/` | durable staging при publish fail |
| failed batches log | `/var/log/spread/failed_batches.log` |

---

## 4. Preconditions (T0 gate — перед стартом overnight)

Все пункты — **read-only / deploy sync**, без destructive cleanup без явного approve.

### Host / disk

```bash
ssh root@38.180.94.108 'hostname; free -h; df -h / /data; ls -la /data'
```

| Gate | Критерий |
|------|----------|
| RAM class | ≥14 GiB MemAvailable idle |
| Disk free `/` или `/data` | ≥**40 GiB** free на старте (80G host; overnight headroom) |
| `/data` state | **fresh collection** — не импортировать thick Aug8+ со старого хоста. Допустимо: пусто **или** уже чистый lean от 16G soak (без orphan experiments) |
| Staging | `/root/spread_staging` синхронизирован с репо (app/, validation/, deploy/, universe CSV) |
| venv | `/root/venv` с pyarrow/pandas/websockets/ccxt |

### Units / MemoryMax (как на 16G soak)

На VPS после soak ожидается (если drift — **выровнять до старта**):

| Unit | Ожидание |
|------|----------|
| `spread-collector.service` | `SPREAD_LEAN_SCHEMA=1`, `SPREAD_COLLECT_BARS=1`, pairs≈337 |
| `spread-compactor.service` | `MemoryMax=2500M`, `--max-windows 1`, own flock plus 90 s shared-lock wait |
| `spread-bars-backup-transfer.service` | `MemoryMax=1500M`, `--max-files 500`, non-blocking shared lock |
| tick + bars backup timers | active после старта |
| **bars timer** | см. §5 — **до** overnight: `OnUnitActiveSec=20min` |

> Репозиторий `deploy/systemd/*` может ещё показывать старые memory limits — на VPS приоритет у **фактически установленных** unit’ов. Перед canary: `systemctl cat` и sync unit-файлов из repo после shared-lock patch.

### rclone / remote

```bash
/opt/rclone-1.74.4/rclone lsd backup1tb: --timeout 60s --retries 1
/opt/rclone-1.74.4/rclone lsl backup1tb:spread-compacted --max-depth 1 | tail
/opt/rclone-1.74.4/rclone lsd backup1tb:spread-bars --max-depth 1
```

| Gate | Критерий |
|------|----------|
| Key | `/root/.ssh/id_ed25519_uploader` root-only |
| Remotes | `spread-compacted` и `spread-bars` listOK |
| Rogue processes | нет чужих `screaner_b_o.py` / hostcap experiments |

### Universe

| Gate | Критерий |
|------|----------|
| ROW / pairs | ≈**337** (как в 16G soak); bars coins ≈336 |
| Schema | lean ticks + bars для тех же монет |

### Preflight snapshot

```bash
cd /root/spread_staging
/root/venv/bin/python validation/ops_alerts.py --once
systemctl status spread-collector.service spread-compactor.timer \
  spread-backup-transfer.timer spread-bars-backup-transfer.timer --no-pager
```

Записать T0 в `/tmp/overnight-canary-20260811-t0.txt`.

---

## 5. Fix bars↔compactor Conflicts thrash (ОБЯЗАТЕЛЬНО до / как старт overnight)

### Residual из 16G soak

После роста bars hive transfer > периода таймера (5 min) mutual `Conflicts=` даёт **SIGTERM thrash** (compactor `status=15/TERM` ×14, bars ×28), не OOM. Итог: `compaction_lag_high` / `compaction_complete_missing`, при том что ручной oneshot compact снова OK.

### Исторические кандидаты

| # | Митигация | Плюсы | Минусы |
|---|-----------|-------|--------|
| A | **`OnUnitActiveSec=20min`** на bars timer | минимальный diff; Conflicts остаётся safety net; прямо бьёт root cause (period ≪ duration) | если один bars run >20 m — thrash может вернуться |
| B | Убрать mutual `Conflicts=`, общий `flock` `/run/spread-heavy.lock` | корректная сериализация без SIGTERM | больше unit/code churn; нужно не сломать tick backup |
| C | Урезать `--max-files` (батчи) | короче каждый bars oneshot | больше запусков → чаще Conflicts-starts; backlog drain медленнее |

### Реализованная рекомендация: shared lock

Удалены mutual `Conflicts=` и добавлен `/run/spread-heavy-storage.lock`.
Compactor сохраняет собственный `/run/spread-compactor.lock`, ждёт shared lock
до 90 s и then emits `compactor_skipped_heavy_storage_busy` without terminating
bars. Bars tries the shared lock non-blocking, emits
`bars_transfer_skipped_busy`, exits successfully and retries on its next
20-minute tick. `BACKUP_TRANSFER_LOCK_PATH` remains responsible for
bars-to-bars exclusivity.

`--max-files 500` is retained because it is the existing canary batch for tiny
hive files; it must not be changed without new duration and backlog evidence.
`Conflicts=` terminates an already-running peer rather than serializing it.
The 90 s bound prevents a long bars run from indefinitely blocking compaction,
but lag recovery/trend remains a verdict gate.

**Не делать перед overnight:** откат `MemoryMax` compactor/bars вниз;
выключение bars timer (это уже не полный контур); ручное удаление backlog.

### Verify после правки (smoke 15–30 m)

```bash
systemctl daemon-reload
systemctl restart spread-compactor.timer spread-bars-backup-transfer.timer
systemctl cat spread-compactor.service spread-bars-backup-transfer.service | grep spread-heavy-storage.lock
# за 60–90m: TERM count не должен расти; bars skip допустим с наблюдаемым backlog
journalctl -u spread-compactor.service -u spread-bars-backup-transfer.service \
  --since '90 min ago' | grep -c 'status=15/TERM' || true
```

Критерий smoke перед overnight: **0** новых oom-kill; TERM count ≪ soak-era rate; ≥1 `compaction_complete` при collector ON.

---

## 6. Overnight procedure

### Timing

| Параметр | Значение |
|----------|----------|
| Старт | вечер оператора (рекоменд. **~21:00–23:00 Europe/Moscow** / документировать UTC) |
| Длительность | **8–12 h** wall-clock (не полный 24h canary_24h.py — systemd prod path) |
| Режим | **systemd** collector + все 3 timers (не `canary_24h.py` process owner) |
| Данные | lean ticks all coins + bars same coins |

### Start order

1. Подтвердить §4 + §5 (bars timer 20 min applied).
2. Убедиться: `spread-compactor.timer`, `spread-backup-transfer.timer`, `spread-bars-backup-transfer.timer` **active**.
3. Idle compact oneshot (опционально) — exit 0, 0 OOM.
4. `systemctl start spread-collector.service` (или уже active — зафиксировать PID).
5. Записать T0 UTC + PID + remote object counts ticks+bars + `df` + `ops_alerts --once`.
6. Запустить sampler (cron/loop) каждые **30 min** → `/tmp/overnight-canary-20260811-evidence.log`.

### Sampling cadence (каждые 30 min)

Собирать в один лог (пример блока):

```bash
TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)
{
  echo "=== $TS ==="
  systemctl is-active spread-collector.service
  systemctl show spread-collector.service -p NRestarts -p MainPID --value
  free -h | head -2
  df -h / /data | tail -n +1
  /root/venv/bin/python /root/spread_staging/validation/ops_alerts.py --once
  grep -c '"event": "compaction_complete"' /var/log/spread/compactor.log || true
  journalctl -u spread-compactor.service --since '30 min ago' --no-pager | grep -c 'oom-kill' || true
  journalctl -u spread-compactor.service --since '30 min ago' --no-pager | grep -c 'status=15/TERM' || true
  journalctl -u spread-bars-backup-transfer.service --since '30 min ago' --no-pager | grep -c 'status=15/TERM' || true
  /opt/rclone-1.74.4/rclone size backup1tb:spread-compacted --json --timeout 120s --retries 1 || true
  /opt/rclone-1.74.4/rclone size backup1tb:spread-bars --json --timeout 120s --retries 1 || true
  # live/bars/sent/archived counts (лёгкий find или du -sh)
  du -sh /data/live /data/live/archived /data/compacted /data/compacted/sent \
        /data/bars /data/bars/sent /data/spool 2>/dev/null
} >> /tmp/overnight-canary-20260811-evidence.log 2>&1
```

### Mid-night (опционально, 1 раз)

Не обязателен для PASS. Если оператор просыпается: только read-only `ops_alerts --once` + `df`. **Не** рестартовать collector «для профилактики».

### Controlled restart (опциональный слот)

Не обязателен, если 16G soak уже доказал restart. Если включать: **один** `systemctl restart spread-collector` mid-run (≥4 h после старта), зафиксировать resume ingest. Fail overnight **не** ставить из-за отсутствия restart, если остальное PASS.

### Stop / morning

Не останавливать collector на утро, если PASS → сразу multi-day. Если Fail → `systemctl stop spread-collector` после сбора evidence; timers можно оставить для drain.

---

## 7. Metrics & evidence

| Метрика | Источник | Ожидание PASS |
|---------|----------|---------------|
| Collector active, NRestarts | systemd | active; NRestarts=0 **или** объяснённый 1 restart |
| schema / pairs / bars | runtime.log | lean, collect_bars=true, pairs≈337 |
| published_rows / bar_published_rows | runtime.log | монотонный рост (reset после restart OK) |
| OOM | journal `oom-kill` | **0** за всю ночь (collector + compactor + bars) |
| `compaction_complete` | compactor.log | регулярные completes; нет тишины > **3×** cycle (~15 m) устойчиво |
| compaction lag | ops_alerts | обычно ≤**30 m**; краткие spikes OK если сходятся |
| archive age | ops_alerts | oldest archive ≪ 36 h alert (retention работает) |
| tick remote growth | rclone size/lsl | objects/bytes ↑ за ночь |
| bars remote growth | rclone size | objects/bytes ↑ за ночь |
| tick backlog | ops_alerts / compacted | обычно ≤20 files / ≤512 MB |
| sent retention | `du` sent/ + age | sent не растёт без bound; prune ~12 h заметен к утру |
| bars↔compactor TERM | journal status=15 | **низкий**: целевой порог ≤**2** TERM/час на пару; лучше 0 |
| disk free trend | df | не монотонно к &lt;10 GiB; alarm &lt;5 GiB |
| MemAvailable | free | устойчиво ≫1 GiB (ожид. ~10 GiB+) |
| watchdog kills | transfer logs / sqlite | 0 (или объяснённые) |
| ENOSPC | transfer logs | 0 |
| failed_batches | failed_batches.log | 0 или объяснено |
| ops_alerts | `--once` утром | `ops_alert_ok` **или** только explained transient |

Evidence pack утром:

- `/tmp/overnight-canary-20260811-t0.txt`
- `/tmp/overnight-canary-20260811-evidence.log`
- `/tmp/overnight-canary-20260811-morning.txt` (финальный snapshot)
- выдержки journal TERM/OOM counts за окно
- `rclone size` ticks+bars T0 vs T_end

---

## 8. Pass / Pass-with-conditions / Fail → unlock multi-day

### PASS → unlock multi-day / weeks unattended

Все обязательно:

1. **OOM = 0** (collector, compactor, bars) за всю ночь.
2. Collector **alive** весь интервал (или 1 documented safe restart + resume).
3. ≥ регулярный поток `compaction_complete` при collector ON; lag **не** залипает >30 m на финале без recovery.
4. Tick remote `backup1tb:spread-compacted` **вырос** vs T0; local → sent → prune наблюдается.
5. Bars remote `backup1tb:spread-bars` **вырос** vs T0; bars sent/prune наблюдается.
6. Disk free тренд **устойчивый** (не к исчерпанию за &lt;48 h экстраполяции).
7. Bars↔compactor **thrash снят**: TERM rate низкий (см. §7); нет устойчивого `compaction_complete_missing` из-за Conflicts.
8. `ops_alerts --once` утром: `ops_alert_ok` или только понятные/закрытые warnings.

После PASS: оставить все units enabled; model track читает backup; оператор — редкие checks (§10 cadence).

### PASS WITH CONDITIONS → multi-day только с оговорками

Любое из:

- редкие TERM (например суммарно ≤5 за ночь) без роста lag на финале;
- краткие ops_alerts spikes, самовосстановившиеся;
- bars remote рост медленнее ожидаемого, но >0 и без ENOSPC;
- disk free ок, но тренд требует более частых df checks первые 48 h.

**Не** unlock «забыть на недели» без follow-up (escalate §5B или bars cadence 30 m).

### FAIL → не unlock multi-day

Любое из:

- любой **oom-kill** / MemoryError storm;
- collector dead / NRestarts storm / StartLimit;
- **0** `compaction_complete` при collector ON на длинном окне (≥1 h тишина без объяснения);
- thrash как в soak (десятки TERM, lag ≫30 m на утро без recovery);
- remote ticks **или** bars не растут при живом ingest (silent loss risk);
- disk free &lt;5 GiB или ENOSPC;
- row_count_match failures / compaction_alert storm не из known stale-path после sent;
- необходимость ручного oneshot compact каждую ночь, чтобы «догонять».

Stop condition mid-run (немедленно остановить collector): OOM storm, disk &lt;3 GiB, ENOSPC, потеря mount/remote недоступен >1 h **и** backlog грозит диском.

---

## 9. Morning checklist (оператор)

```bash
ssh root@38.180.94.108
cd /root/spread_staging

# 1) units
systemctl status spread-collector.service --no-pager
systemctl list-timers 'spread-*' --no-pager

# 2) alerts + disk + mem
/root/venv/bin/python validation/ops_alerts.py --once
free -h; df -h / /data

# 3) OOM / TERM overnight window (подставить T0)
journalctl -u spread-compactor.service -u spread-collector.service \
  -u spread-bars-backup-transfer.service --since '12 hours ago' \
  | grep -E 'oom-kill|status=15/TERM' | wc -l

# 4) compaction health
grep '"event": "compaction_complete"' /var/log/spread/compactor.log | tail -5

# 5) remote growth
/opt/rclone-1.74.4/rclone size backup1tb:spread-compacted --json --timeout 120s
/opt/rclone-1.74.4/rclone size backup1tb:spread-bars --json --timeout 120s

# 6) folder transitions
du -sh /data/live /data/live/archived /data/compacted /data/compacted/sent \
      /data/bars /data/bars/sent /data/spool

# 7) bars timer still 20min
systemctl cat spread-bars-backup-transfer.timer | grep OnUnitActiveSec
```

Записать вердикт: **PASS / PASS WITH CONDITIONS / FAIL** в короткий note (можно новый `docs/overnight-prod-canary-20260811-result.md` **после** execute — не сейчас).

---

## 10. Multi-day cadence (после PASS)

| Когда | Что |
|-------|-----|
| Раз в день (5 min) | `ops_alerts --once`, `df -h /`, `systemctl is-active spread-collector` |
| Раз в 2–3 дня | `rclone size` ticks+bars; NRestarts; TERM/OOM journal since yesterday |
| Раз в неделю | spot-check parquet read 1 tick + 1 bar remote; sent prune still bound |
| Escalate | disk &lt;10 GiB, любой OOM, lag &gt;60 m устойчиво, remote flat |

---

## 11. Как model track потребляет backup

См. также [`docs/model-data-sources.md`](model-data-sources.md).

| Слой | Remote | Локальный pull (research machine) |
|------|--------|-----------------------------------|
| Lean ticks compacted | `backup1tb:spread-compacted/spread_*.parquet` | `rclone copy backup1tb:spread-compacted ./research_data/ticks/ --include "spread_*.parquet"` |
| Bars hive `bar_5m` | `backup1tb:spread-bars/bar_5m/...` | `rclone copy backup1tb:spread-bars ./research_data/bars/` |

Инварианты для модели:

- Model **не** читает hot `/data/live` на VPS как source of truth.
- Смешивать canary-era v1 и lean в одном concat нельзя без schema filter (см. model-data-sources).
- Overnight PASS доказывает **поставку** данных; не доказывает edge/PnL.

---

## 12. Что мы НЕ будем утверждать

Даже при PASS overnight:

1. Не «latency/freshness production-ready» (D/B out of scope).
2. Не «модель прибыльна» / live-bot ready.
3. Не «старый 2 GiB host снова OK» — он retired.
4. Не «миграция backlog со старого хоста валидирована» — мы её не делаем.
5. Не READY по старому `canary_24h.py` accounting протоколу без отдельного account run.
6. Не бесконечная durability без редких checks — multi-day = occasional ops_alerts, не zero-touch forever.
7. Не снятие `--max-windows 1` / MemoryMax caps без новой evidence.

---

## 13. Key risks / failure modes

| Risk | Почему | Mitigation в дизайне |
|------|--------|----------------------|
| Bars↔compactor SIGTERM thrash | 16G residual | §5 A: 20 min bars timer **до** старта |
| Disk fill от archive+sent | 24h canary history на маленьком диске | 80G + retention; df trend; stop &lt;3 GiB |
| Silent backup stall | rclone/SFTP | rclone size T0/Tend; watchdog alerts |
| Compaction lag от thrash | Conflicts | cadence fix; PASS требует lag recovery |
| OOM regression | memory leak / window thickening | MemoryMax + journal; FAIL on any OOM |
| Operator confusion canary_24h vs systemd | два протокола | overnight = **systemd units**, не canary_24h owner |
| Accidental migrate thick backlog | диск/OOM | explicit greenfield; no copy from 38.244 |

---

## 14. Minimal patch / experiment plan (порядок)

1. **Design** (этот документ) — сейчас.  
2. **Ops patch:** bars timer `OnUnitActiveSec=20min` в репо + deploy на VPS; smoke 15–30 m.  
3. **Execute overnight** (только по запросу пользователя): start order §6, sampler 30 m.  
4. **Morning verdict** §8–9 → PASS unlock multi-day.  
5. Если PASS WITH CONDITIONS thrash residual → escalate §5B (flock, drop Conflicts).

Implementing agent (когда execute): Runtime Storage / Validation.  
Review gate: Review Critic на unit timer change; Validation на morning evidence.  
Frozen areas: не трогать.

---

## 15. VPS/storage validation plan (summary)

| Слой | Доказательство |
|------|----------------|
| Runtime | collector active, runtime.log publish counters |
| Local FS transitions | live → archived → compacted → sent sizes |
| Compaction | completes + lag + 0 OOM |
| Tick durable | rclone size growth `spread-compacted` |
| Bars durable | rclone size growth `spread-bars` |
| Alerts | ops_alerts snapshot cadence + morning |
| Thrash | journal TERM counts vs soak baseline |

Local laptop checks **не** считаются proof. Только VPS + remote rclone.

---

## 16. Success criteria (коротко)

**Overnight PASS** = 8–12 h полный контур lean+bars N≈337 на `38.180.94.108` с 0 OOM, растущим remote ticks+bars, работающими sent prune, и **снятым** Conflicts thrash после bars timer=20 min → **unlock multi-day unattended** с daily `ops_alerts`.

---

## 17. Recommended next step

1. Согласовать этот design (оператор).  
2. По запросу: применить §5 A (timer 20 min) + короткий smoke.  
3. По запросу: стартовать overnight §6.  
4. Утром: вердикт §8; при PASS — оставить units и перейти на daily checks; model тянет `backup1tb:spread-compacted` + `backup1tb:spread-bars`.
