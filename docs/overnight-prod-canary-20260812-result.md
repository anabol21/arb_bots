# Overnight production canary — итог (2026-08-12)

> **Строгий вердикт для полного контура N=337 (lean ticks + bars): `NO-GO`.**
> Ticks прошли canary и сами по себе готовы к продолжению с обычным
> наблюдением; bars не доказали доставку свежих данных до durable remote и
> продолжают накапливаться локально.

## 1. Блок конвейера

Collection and storage на VPS `root@38.180.94.108`: collector → локальные
`/data/live` и `/data/bars` → compaction → backup transfer → remote
`backup1tb`. Замороженные ingest, parsing, spread calculation и trading logic
не изменялись.

## 2. Среда и доказательства

| Граница | Значение |
|---|---|
| Код | локальный репозиторий; во время оценки не менялся runtime |
| Исполнение | VPS `root@38.180.94.108`, staging `/root/spread_staging` |
| Runtime logs | `/var/log/spread/runtime.log`, `/var/log/spread/compactor.log`, transfer journals |
| Первая материализация | VPS-local `/data/live`, `/data/bars` |
| Durable ticks | `backup1tb:spread-compacted` |
| Durable bars | `backup1tb:spread-bars` |
| Основное evidence | `/tmp/overnight-prod-canary-20260812.log` |

Оценка использовала только bounded read-only SSH probes, evidence и systemd
journals. Сервисы, данные, remote и mount не менялись.

## 3. Завершение sampler и collector

- Transient unit `overnight-prod-canary-20260812.service` был уже выгружен
  systemd (`LoadState=not-found`), но сохранённый результат — `Result=success`,
  `ExecMainCode=0`, `ExecMainStatus=0`.
- Evidence начинается `2026-08-11T23:15:51Z`, содержит финальный snapshot ровно
  в `2026-08-12T07:15:51Z` (`elapsed_sec=28800`) и
  `CANARY_COMPLETE` в `2026-08-12T07:16:44Z`. Значит sampler прожил полный
  плановый восьмичасовой интервал и закончил logging на 53 s позже цели.
- 17 snapshots (T0 + каждые ~30 min + T_end), 17 `ops_alert_ok`.
- Collector оставался `active/running`, PID `24505`, `NRestarts=0`. Heartbeat
  показывает `pairs=337`, `collect_bars=true`, `failures=0`,
  `quarantined_records=0`; published rows выросли с 119.9M до 153.8M, bars
  rows — с 98.0k до 130.5k.

## 4. OOM, TERM и локальная запись

- OOM: **0** в evidence и в journal четырёх релевантных units.
- `status=15/TERM` / SIGTERM: **0** за окно; soak-era thrash не повторился.
- Collector publish failures, rejected/quarantined records и ненулевой spool:
  **0**. В финале spool также `0` files / `0` bytes.
- Есть серии ошибок `OKX candle5m error: no close frame received or sent`
  около `07:13` и `07:16Z`. Они не перезапустили collector и не отразились в
  publish/spool counters, но это не является доказательством полноты bars.

## 5. Compaction и ticks

Compaction прошла ровно и без устойчивого lag:

- lag во всех snapshots: **1.035–6.702 min** (финал 1.035 min);
- `compaction_complete_age_sec`: **4–363 s**; следовательно, не было тишины
  более 15 min;
- `archive_retention_complete_age_sec`: **4–145 s**, а archive oldest age
  оставался около 9.3–12.0 h;
- tick backlog: 0–2 files / 0–21.5 MiB; финал 2 files / 21.4 MiB.

За окно `spread-compactor.service` имеет 240 завершений без failures, а tick
transfer — **96 successful / 0 failed**. Локальный lifecycle ticks работал:
live → archived → compacted → sent; `sent` менялся 164 → 155 files, а
archive/sent bytes снижались на фоне retention/prune. Это ожидаемая
не-монотонность локального sent, а не потеря само по себе.

Durable-подтверждение ticks есть: bounded remote count top-level objects
`spread-compacted` вырос **1665 → 1853 (+188)**. Поэтому для **ticks** verdict
— **READY для days/weeks с обычным daily check**, а не «zero-touch forever».

## 6. Bars: transfer выполняется, durable freshness не доказана

`spread-bars-backup-transfer.service` запускался и завершался успешно
**24 / 24**, failures и TERM — 0. Однако успешный exit unit не равен
доказанной свежей remote publication.

Наблюдаемая локальная очередь bars ухудшилась за 8 h:

| Метрика | T0 | T_end | Изменение |
|---|---:|---:|---:|
| backlog files | 69,525 | 90,927 | +21,402 |
| backlog size | 237.159 MiB | 310.163 MiB | +73.004 MiB |
| oldest age | 1,586.076 min | 2,066.030 min | +479.954 min |
| local `bars_sent` | 488 | 47 | retention/prune виден, но не подтверждает remote freshness |

Remote coverage есть: bounded `lsf --dirs-only --max-depth 1` стабильно
возвращал **336** `base_coin=` directories, то есть все ожидаемые bars coins.
Но bounded freshness probe для BTC, ETH и SOL на T0, T_end и при повторной
проверке на момент оценки возвращал тот же newest partition:
`event_date=2026-08-05` (42 / 44 / 42 objects). Он старее canary на неделю и
не показывает никакого роста.

## 7. Disk и memory

- Disk free `/`/`/data`: **60.603 → 61.669 GiB** во время canary; текущий
  bounded check — 62 GiB free. Нет тренда к исчерпанию.
- `MemAvailable`: около **13 GiB** в snapshots; текущий check — 14 GiB.
  OOM и swap use не наблюдались.

## 8. Ограничение remote inventory

Глобальный `rclone size backup1tb:spread-bars` намеренно не запускался: он
ранее был слишком медленным и нарушил бы bounded probe. Это **не** блокирует
READY для ticks, потому что их remote object count вырос.

Для bars ограничение не является единственной причиной `NO-GO`: уже
получены отрицательные lightweight evidence — все 336 top-level coin
directories существуют, но три детерминированных latest-partition samples
остаются на `2026-08-05`, а local bars backlog растёт. Глобальный inventory мог
бы дать больше деталей, но не мог бы превратить эти факты в READY.

## 9. Итоговый verdict и следующий шаг

| Область | Verdict | Обоснование |
|---|---|---|
| Ticks | **READY** с daily `ops_alerts`/`df` и периодической remote проверкой | 8 h continuity, 0 OOM/TERM/restarts/spool, bounded compaction, remote +188 |
| Bars | **NO-GO** | backlog +21,402, oldest age +480 min, remote samples stale Aug 5; 24 успешных unit exits не доказывают durable свежесть |
| Полный fresh N=337 ticks+bars | **NO-GO** для unattended days/weeks | bars durable boundary не пройдена |

Минимальный следующий шаг — отдельная read-only диагностика semantic gap
«bars transfer `success` → remote freshness»: сопоставить один конкретный
локальный recent bars object с manifest/transfer log и его exact remote path,
после чего выполнить новый canary только после исправления причины. До этого
оставлять полный контур без присмотра на дни/недели нельзя.
