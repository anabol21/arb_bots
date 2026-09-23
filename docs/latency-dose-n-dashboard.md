# Дашборд: E6-lite dose-response N (новый хост)

> ## OWNERSHIP — FINISHED `dose_n50_prod337_20260811d`
>
> Эксперимент завершён; владение окончилось в `2026-08-11T14:55:48Z`. Артефакты остаются
> историческими и доступны только для чтения: не удалять, не обнулять, не ротировать и не
> уплотнять `/data/experiments/dose_n50_prod337_20260811d` и связанные журналы без отдельного решения.
> Production `spread-collector` PID `24505` наблюдался только для чтения и не был остановлен либо
> перезапущен.
>
> | Объект | Фактический итог | Путь |
> |---|---|---|
> | supervisor `166358` | `inactive`, `Result=success`, `ExecMainStatus=0` | `/var/log/spread/dose_n50_prod337_20260811d_supervisor.log` |
> | N=50 shadow `166387` | после ping: штатный flush, 564 558 строк, 5 650 файлов, `failures=0` | `/data/experiments/dose_n50_prod337_20260811d/{live,spool}` |
> | XRP ping `166430` | `14:20:53.836Z` → `14:50:55.792Z`, `duration_elapsed` | `/var/log/spread/dose_n50_prod337_20260811d_ping_dual.log` |
> | production N≈337 `24505` | `active` при проверке `15:02:12Z`; XRP parquet прочитан в том же окне | `/data/live/base_coin=XRP/**` |

> ## OWNERSHIP — dose-N `dose_n_20260811c` (historical)
>
> **Владелец / Owner:** Track **(D) latency dose-response** / Runtime+Validation. Статус: **`finished_with_measurement_failure`**; владение окончено.  
> Историческая защита серии сохраняется: не изменять raw артефакты без отдельного согласования. `dose-n-supervisor-20260811c.service`
> больше не активный объект эксперимента; после второго prod-ping он завершился `failed` с синтаксической ошибкой wrapper, production не затронут.  
> **Исторический DO NOT TOUCH:** `dose-n-supervisor-20260811c.service`, его PID/children,
> `/var/log/spread/dose_n*20260811c*`, `/var/log/spread/DO_NOT_TOUCH_LATENCY_DOSE_20260811C.txt`,
> `/data/experiments/dose_n{3,10,337_prod}_20260811c`. Другим Track (D) ветвям (включая
> compaction/backup/retention) разрешено только чтение до series end: **не** kill/restart,
> truncate/rotate, delete/compact/reclaim.  
> Markers: `/var/log/spread/DO_NOT_TOUCH_LATENCY_DOSE_20260811C.txt`,
> `/data/experiments/dose_n{3,10,337_prod}_20260811c/DO_NOT_TOUCH.md`.  
> Launch: persistent explicit systemd unit + wrapper `setsid` children; status and exact times in
> [`latency-dose-n-run-live.md`](latency-dose-n-run-live.md). Итог и артефакты: [`latency-dose-n-results.md`](latency-dose-n-results.md).

**Текущий статус.** Серия завершена и является историческим read-only набором. К `N=1/3/10` добавлена теневая точка `N=50` и contemporaneous production-наблюдение `N≈337`. Хвоста нет до 50, а в production ≈337 он наблюдается; подробные цифры и границы вывода находятся в [`latency-dose-n-results.md`](latency-dose-n-results.md). Это направленный E6-lite, не полный 5×1h E6 из канона.

| Поле | Значение |
|------|----------|
| Трек | (D) latency · Orchestrator + Validation |
| Канон | [`latency-root-cause-experiments.md`](latency-root-cause-experiments.md) E6 / H1 · E2b [`latency-e2b-results-20260810.md`](latency-e2b-results-20260810.md) · host compare [`latency-host-compare-20260810.md`](latency-host-compare-20260810.md) |
| Хост | **только NEW** `root@38.180.94.108` |
| Режим | **E6-lite** — ~25 мин steady на руку (не 2h); directional |
| Код | `/root/spread_staging` — **env-only**, ingest/parse/spread frozen |
| Результаты | [`latency-dose-n-results.md`](latency-dose-n-results.md) — `N=1/3/10/50` valid; `N≈337` = `valid_production_observation` с конфаундерами |
| Вне области | compaction, backup, retention, truncate logs, kill prod, патч WS |

---

## 1. Pipeline block

```text
  OKX/Bybit books ×N  →  shadow screaner_b_o (lean, bars off)
                              └─ delivery_latency_ms = local_recv − exchange_ts (XRP probe)
  matched ping XRP    →  ping_okx_bybit_2h.py (per-N window)
  large-N arm         →  prod N=337 (bars on) XRP lean ∩ same ping  [не второй shadow]
```

Метрика (не менять): trigger-leg `delivery_latency_ms`. Bybit ping: `age_ts_ms`.

---

## 2. Гипотезы и фальсификаторы

| ID | Утверждение | Предсказание | Фальсификатор |
|----|-------------|--------------|---------------|
| **H1-dose** | хвост растёт с N (fan-out) | N=1≈ping; N=3/10 мягкий рост; N≈337 ≫ ping (p99 сотни–тысячи мс / dual>1 с) | p99 S/P ≈1× на всех N включая 337 → не чистый H1 |
| **H_threshold** | тяжёлые хвосты появляются начиная с некоторого порядка N | порог между 10 и 337 (или раньше) | монотонный рост уже на N=3–10 до p99≳500 мс **или** скачок только на 337 |
| **H_coexist** | соседний prod искажает малые N | N=1 на NEW всё ещё ≈ping (как hostcap-c) | N=1 shadow ≫ ping при тихом ping → confound coexistence / process |

**Запрет вывода:** короткий smoke → «gate #1 закрыт» / «шардировать обязательно».

---

## 3. Дизайн решения (зафиксировано)

1. **Хост:** NEW only.
2. **Расписание:** **последовательно** `N=1 → 3 → 10`, затем large-N. Не параллелить shadow-руки.
3. **Large-N (вместо shadow N=330):** prod уже `Loaded pairs: 337`, bars on → **не** запускать второй full-N. Рука `dose_n337_prod_*`: только matched ping + анализ prod `/data/live/.../XRP` за то же окно. Confounders документировать (bars on, `PERSIST_EVERY` default≠5000).
4. **Окно:** `DURATION_SEC=1500` (~25 мин) на руку; steady = отбросить первые 5 мин.
5. **Ping:** новый процесс на каждую руку (clean overlap).
6. **Universe:** contiguous slice с XRP в конце (индекс 329):

| N | `SPREAD_ROW_START` | `SPREAD_ROW_END` | first…last |
|---|-------------------:|-----------------:|------------|
| 1 | 329 | 330 | XRP |
| 3 | 327 | 330 | XLM…XRP |
| 10 | 320 | 330 | WLD…XRP |
| ≈337 | prod full | — | prod universe (XRP probe) |

7. **`SPREAD_PERSIST_EVERY=5000`** на всех **shadow** руках (как E2/hostcap) — flush rate не confounder между shadow-N. Prod large-N — отдельная оговорка.
8. **Bars:** shadow `SPREAD_COLLECT_BARS=0`; prod large-N имеет bars on (оговорка H2).

---

## 4. Env shadow (шаблон)

```bash
N=1   # или 3 / 10
START=$((330 - N))   # 329 / 327 / 320
END=330
RUN_ID=dose_n${N}_20260811b
ROOT=/data/experiments/${RUN_ID}

SPREAD_ROW_START=$START
SPREAD_ROW_END=$END
SPREAD_COLLECT_BARS=0
SPREAD_LEAN_SCHEMA=1
SPREAD_PERSIST_EVERY=5000
SPREAD_PARQUET_ROOT=${ROOT}/live
SPREAD_SPOOL_ROOT=${ROOT}/spool
SPREAD_RUNTIME_LOG=/var/log/spread/${RUN_ID}_runtime.log
SPREAD_FAILED_BATCHES_LOG=/var/log/spread/${RUN_ID}_failed_batches.log
```

Python: `/root/spread_venv/bin/python` (shadow) · ping тот же · prod unit — `/root/venv` (не трогать).

---

## 5. Команды / supervisor

Supervisor на VPS: `/root/spread_staging/validation/dose_n_supervisor.sh`  
Статус: `/var/log/spread/dose_n_supervisor.status`  
Лог: `/var/log/spread/dose_n_supervisor.log`

Ручной одиночный запуск (эквивалент одной руки):

```bash
# chrony check
chronyc tracking | egrep 'System time|Leap'

# dirs + marker
# предпочтительно: systemd-run unit dose-n-supervisor (серия 20260811b уже запущена)
# ручной одиночный arm — только если supervisor не используется:
mkdir -p /data/experiments/dose_n1_20260811b/{live,spool} /var/log/spread
cd /root/spread_staging
setsid env SPREAD_ROW_START=329 SPREAD_ROW_END=330 \
  SPREAD_COLLECT_BARS=0 SPREAD_LEAN_SCHEMA=1 SPREAD_PERSIST_EVERY=5000 \
  SPREAD_PARQUET_ROOT=/data/experiments/dose_n1_20260811b/live \
  SPREAD_SPOOL_ROOT=/data/experiments/dose_n1_20260811b/spool \
  SPREAD_RUNTIME_LOG=/var/log/spread/dose_n1_20260811b_runtime.log \
  SPREAD_FAILED_BATCHES_LOG=/var/log/spread/dose_n1_20260811b_failed_batches.log \
  /root/spread_venv/bin/python app/screaner_b_o.py \
  > /var/log/spread/dose_n1_20260811b_shadow.nohup.out 2>&1 </dev/null &
```

Smoke: `Loaded pairs: N`; XRP subscribed; parquet под `base_coin=XRP`.

Остановка shadow после окна: `kill -TERM <shadow_pid>`; ждать `shutdown_flush_done`. **Не** `systemctl stop spread-collector`.

---

## 6. Расписание (план)

| Порядок | Рука | Тип | Длит. | Соседство с prod |
|--------:|------|-----|------:|------------------|
| 1 | `dose_n1_20260811b` | shadow+ping | ~25 мин | рядом с prod full-N (как hostcap-c) |
| 2 | `dose_n3_20260811b` | shadow+ping | ~25 мин | то же |
| 3 | `dose_n10_20260811b` | shadow+ping | ~25 мин | то же |
| 4 | `dose_n337_prod_20260811b` | **ping only** ∩ prod XRP | ~25 мин | сам prod = S |

ETA серии ≈ 4 × 25 мин + паузы flush ≈ **~2 ч** с `23:36Z` → ~`01:19Z`.  
Abort `dose_n1_20260811` (без суффикса b) — не канон для вердикта.

---

## 7. Анализ

После каждой руки (или в конце): квантили S vs P на steady (минус 5 мин), dual>500/1000 мин, отношение S/P p99.  
Скрипт: `validation/dose_n_analyze.py` (локально/на VPS read-only).  
Таблица — [`latency-dose-n-results.md`](latency-dose-n-results.md).

---

## 8. Риски

| Риск | Митигация |
|------|-----------|
| OOM / CPU при shadow N=10 + prod 337 | RAM 15 GiB; N≤10 shadow; не стакать N=330 |
| Prod coexistence на малых N | документировать; N=1 якорь vs hostcap-c |
| Large-N bars on / другой PERSIST | пометить руку `prod` отдельно от shadow dose |
| Clock | chrony verify перед стартом серии |
| Чужой kill shadow | DO_NOT_TOUCH + ownership sticky |

---

## 9. Success criteria

1. Сопоставимые shadow-точки `1 / 3 / 10 / 50` имеют полный overlap и steady более 20 минут — **выполнено**.
2. Таблица p50/p95/p99/max S и P + dual counts полна для shadow-точек и production-наблюдения — **выполнено**.
3. Вердикт направленный: тяжёлый хвост ограничен интервалом `50 < N ≤ 337`; `gate #1` не закрыт — **выполнено с оговорками**.
4. Prod unit не остановлен этим экспериментом; production анализировался только для чтения — **выполнено**.

---

## 10. Следующий шаг после серии

1. [`latency-dose-n-results.md`](latency-dose-n-results.md) фиксирует полный N=50 и production-coverage.
2. Следующая единственная точка — изолированный shadow `N=150` с теми же настройками и свежим ping.
3. `gate #1` остаётся открыт.
