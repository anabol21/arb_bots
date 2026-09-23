# Пробный прогон: мощность хоста (H_host)

**Трек:** (D) latency · Orchestrator + Validation  
**Дата запуска:** 2026-08-10  
**Связанные документы:** [`latency-e2-dashboard.md`](latency-e2-dashboard.md) · [`latency-e2-run-live.md`](latency-e2-run-live.md) · [`latency-root-cause-experiments.md`](latency-root-cause-experiments.md)

**Статус (2026-08-10 ~21:15Z):** hostcap-**c** **complete** (`21:08:50Z` stop). Сравнение OLD vs NEW — [`latency-host-compare-20260810.md`](latency-host-compare-20260810.md). **H_host при N=1 ослаблена** (S≈P на обоих хостах; ping floor NEW≈OLD после chrony). E2b на old ещё мог быть live до `21:41:36Z` — не kill.

---

## 1. Гипотеза H_host

**Утверждение.** Большие хвосты latency на matched XRP в полном prod-скринере на старом VPS объясняются **недостаточной мощностью хоста** (CPU/RAM/steal/IO contention) при полной нагрузке, а не только fan-out подписок (H1).

**Отличие от H1 (E2):**

| | H1 (E2) | H_host (этот trial) |
|--|---------|---------------------|
| Вопрос | налог от N book-listeners на **том же** хосте | налог от **класса машины** при том же N=1 / том же коде |
| Контроль | shadow N=1 vs ping на old VPS | тот же N=1 + ping на **новом** fat host vs числа E2 на old |
| Фальсификатор | N=1 shadow ≫ ping → не чистый fan-out | см. §5 |

---

## 2. Хосты

| Поле | Old VPS (E2b) | New VPS (hostcap trial) |
|------|---------------|-------------------------|
| SSH | `root@38.244.198.42` | `root@38.180.94.108` |
| Hostname | `a696137333.local` | `a845945761.local` |
| CPU | 2 × Xeon Gold 6154 @ 3.00 GHz | 6 × Xeon Gold 6326 @ 2.90 GHz |
| RAM | 1.9 GiB | 15 GiB |
| Load (снимок) | ~1.2 при E2b+соседях | ~0.4 до старта prod; выше после |
| Prod collector | `inactive` (не трогали) | стал `active` @ `20:41:42Z` (чужой деплой; **не** останавливали) |
| Роль сейчас | E2b до `21:41:36Z` (на момент compare — partial) | hostcap-c **done**; prod side-by-side |
| Время | — | **chrony** enabled/active (UTC); см. §3.0 |

Источник identity нового хоста: [`vps-runbook.md`](vps-runbook.md), [`program-roadmap.md`](program-roadmap.md) (миграция 2026-08-10).

---

## 3.0 Ops: синхронизация времени (новый сервер)

**Зачем.** Latency = `local_ms − exchange_ts`. Без NTP/chrony «сетевой floor» и хвосты shadow/ping смещены на offset часов. Пользователь явно попросил синхронизировать время **до** запуска скрипта.

### Что было не так (before)

| Проверка | Результат ~`20:46Z` (до `apt install chrony`) |
|----------|-----------------------------------------------|
| `date -u` | `2026-08-10 20:46:33 UTC` (совпадало с wall-clock) |
| `timedatectl` | TZ=`Etc/UTC`; `System clock synchronized: yes`; `NTP service: active` |
| Сервис | только `systemd-timesyncd` (active since boot `20:13:01Z`) |
| Offset | `timedatectl timesync-status` → **Offset ≈ −17.8 ms**, Delay ≈ 234 ms, Jitter ≈ 16 ms |
| chrony / ntpsec | **не установлены** |
| Конфиг | `/etc/systemd/timesyncd.conf` — всё закомментировано (дефолт `ntp.ubuntu.com`) |
| Журнал | свежий boot `20:12Z`; до этого hostname `ubuntu.localdomain` (May 22) — типичный «голый» VPS без долгой стабильной sync-истории |

Итого: timesyncd уже крутился и offset был небольшой (~18 ms), но **постоянного chrony не было**, конфиг не закреплён, а runs **a/b** стартовали без явной проверки/форс-sync. Для latency-trial это недостаточно жёстко.

### Что сделано (config applied)

1. `apt-get install -y chrony` → пакет `chrony 4.2-2ubuntu2` (Ubuntu 22.04).
2. `systemd-timesyncd` **удалён** как конфликтный dependency при установке chrony; unit `masked`/`inactive`.
3. `timedatectl set-timezone Etc/UTC`
4. `systemctl enable --now chrony` → `enabled` + `active`
5. Форс-шаг: `chronyc makestep` + `chronyc waitsync`
6. Конфиг по умолчанию Ubuntu: pools `ntp.ubuntu.com` / `*.ubuntu.pool.ntp.org`, `makestep 1 3`, `driftfile /var/lib/chrony/chrony.drift`

**Не трогали:** SSH keys, mount configs, compaction/backup units, prod `spread-collector`, old VPS E2b (`38.244.198.42` PIDs `1264180`/`1264183`).

### Верификация (after)

Команды:

```bash
ssh root@38.180.94.108 'date -u; timedatectl status; chronyc tracking; chronyc sources; systemctl is-active chrony'
```

Сводка ~`2026-08-10T20:48:40Z`:

| Поле | Значение |
|------|----------|
| `date -u` | `Mon Aug 10 20:48:40 UTC 2026` |
| TZ | `Etc/UTC` |
| `System clock synchronized` | **yes** |
| chrony | `active` / `enabled` |
| Leap status | **Normal** |
| System time vs NTP | **≈ +0.6 ms** (fast) |
| Last offset | **≈ +0.85 ms** |
| Stratum | 3 |
| Best source | `*` static-DIA-… (pool) |

### Следствие для latency smoke

Сразу после фикса ping на том же хосте показал Bybit ≈21 ms / OKX ≈29–30 ms (раньше в §6 smoke b: Bybit p50≈61 / OKX≈69). Часть «высокого network floor» на a/b могла быть **clock confound**, не маршрутом. Для вердикта H_host опираться на **run c** (и ratios), артефакты a/b помечать как pre-chrony / dirty.

---

## 3. Что запущено

**Минимум информативный:** matched `ping_dual` XRP ~20 мин + shadow N=1 lean bars-off, отдельный experiment root.

### 3.1 Первый запуск (a) — aborted

| Поле | Значение |
|------|----------|
| Run id | `hostcap_n1_xrp_20260810` |
| Start UTC | `2026-08-10T20:39:21Z` |
| Shadow PID | `4539` |
| Ping PID | `4542` (продолжает жить) |
| Abort | `20:41:33Z` — `shutdown signal received` (SIGTERM) |
| Причина (факт по времени) | через ~9 с стартовал `spread-collector.service` (`20:41:42Z`, Main PID `5276`, `Loaded pairs: 337`); кто-то поднял prod и зачистил чужой `screaner_b_o.py` |
| Артефакт | 1 parquet, 1791 rows в `/data/experiments/hostcap_n1_xrp_20260810/live/` |
| Clock note | pre-chrony (только timesyncd) |

### 3.2 Релонч (b) — stopped (clock resync)

| Поле | Значение |
|------|----------|
| Run id | `hostcap_n1_xrp_20260810b` |
| Host | `root@38.180.94.108` |
| Start UTC | `2026-08-10T20:42:25Z` |
| Expected end UTC | `2026-08-10T20:58:50Z` (~985 s) |
| Shadow PID | `5704` |
| Ping PID | `4542` (reuse) |
| Autostop PID | `5707` |
| Stop | ~`20:47:50Z` — чистый `TERM` shadow+ping+autostop **после** установки chrony; причина: relaunch с хорошими часами (run `c`) |
| Prod | Main PID `5276` остался `active` |
| Paths | `/data/experiments/hostcap_n1_xrp_20260810b/{live,spool}` |
| Clock note | pre-chrony install / dirty для абсолютных мс |

### 3.3 Релонч (c) — complete (после фикса часов)

| Поле | Значение |
|------|----------|
| Run id | `hostcap_n1_xrp_20260810c` |
| Host | `root@38.180.94.108` |
| Start UTC | `2026-08-10T20:48:20Z` |
| Expected end UTC | `2026-08-10T21:08:20Z` (1200 s) |
| Actual end | ping `finished` `duration_elapsed` @ `21:08:21Z`; shadow TERM @ `21:08:50Z` (`shutdown_flush_done`, rows=18107) |
| Shadow PID | `7631` (gone) |
| Ping PID | `7630` (gone) |
| Autostop PID | `7632` (gone; wrote `stopped_at=2026-08-10T21:08:50Z`) |
| Python | `/root/spread_venv/bin/python` |
| Code | `/root/spread_staging` |
| Parquet / spool | `/data/experiments/hostcap_n1_xrp_20260810c/{live,spool}` |
| Shadow log | `/var/log/spread/hostcap_c_n1_xrp_runtime.log` |
| Ping log | `/var/log/spread/hostcap_c_n1_xrp_ping_dual.log` |
| Local copy | `output/hostcap_c/` |
| Markers | `.../hostcap_n1_xrp_20260810c/DO_NOT_TOUCH.md` · `/var/log/spread/HOSTCAP_LATENCY_OWNED.txt` |
| Prod | `spread-collector` **active** PID `5276` → `/data/live`; **не** трогали |
| Clock | chrony Normal, offset ≲1 ms до старта и после стопа |

### Env shadow (c, изолированный)

```bash
SPREAD_ROW_START=329 SPREAD_ROW_END=330   # XRP, N=1
SPREAD_COLLECT_BARS=0
SPREAD_LEAN_SCHEMA=1
SPREAD_PERSIST_EVERY=5000
SPREAD_PARQUET_ROOT=/data/experiments/hostcap_n1_xrp_20260810c/live
SPREAD_SPOOL_ROOT=/data/experiments/hostcap_n1_xrp_20260810c/spool
SPREAD_RUNTIME_LOG=/var/log/spread/hostcap_c_n1_xrp_runtime.log
SPREAD_FAILED_BATCHES_LOG=/var/log/spread/hostcap_c_n1_xrp_failed_batches.log
```

### Smoke (c, факт)

- `Loaded pairs: 1`; Bybit+OKX XRP subscribed; lean; bars off; root=`.../hostcap_n1_xrp_20260810c/live`
- ping `remaining_sec≈1180` @ `20:48:40Z`; Bybit≈21 ms, OKX≈29 ms
- prod `active` PID `5276` — coexistence; пути не пересекаются с `/data/live`
- E2b на old VPS **не трогали**

### Bootstrap gap (закрыт ранее)

На новом хосте не было рабочего venv при первом заходе. Создан `/root/spread_venv` с пакетами как на old: `websockets`, `websocket-client`, `pandas`, `pyarrow`, `numpy`, `orjson`, `aiohttp`, `uvloop`, `requests`. Позже появился и `/root/venv` (используется systemd prod).

---

## 4. Статус E2b (old VPS) — ownership

На момент сравнения (~`21:14Z`) E2b ещё **running** (не kill). Для OLD N=1 использованы **E2b partial** (~92 мин wall / ~87 мин steady) + prior E2 short как ref.

| Поле | Значение |
|------|----------|
| Expected end | `2026-08-10T21:41:36Z` |
| Shadow PID `1264180` | leave alone |
| Ping PID `1264183` | leave alone |
| Local partial | `output/e2b_n1_xrp/` (`*_PARTIAL.*`) |
| Ownership | `/var/log/spread/E2_LATENCY_OWNED.txt` — **не трогали** |

---

## 5. Фальсификаторы H_host — итог после hostcap-c

Полные таблицы: [`latency-host-compare-20260810.md`](latency-host-compare-20260810.md).

1. **H_host ослаблена (для N=1):** на fat host shadow **не** ≫ ping (OKX/Bybit p99 S/P ≈ **1.00× / 1.04×**). Абсолютные S не лучше OLD на порядок (OLD E2b partial ≈ 1.06× / 1.00×).
2. **Частичный сценарий §5.2 активен:** оба хоста при N=1 ≈ ping → следующий шаг = **full-N / prod XRP на NEW** vs ping.
3. **Clock confound a/b подтверждён:** pre-chrony ping p50 ~69/61 → post-chrony ~29/20 (как OLD).

H_host **не** объясняет prod-хвосты при N=1 и **не** отменяет E2/H1. `gate #1` **не** закрыт.

---

## 6. Результаты hostcap-c (не smoke)

### 6.1 Ping pre-chrony (b, dirty) — только контекст

| Нога | n | p50 | p95 | p99 | max |
|------|---|-----|-----|-----|-----|
| OKX | 582 | 69 | 71 | 73 | 99 |
| Bybit | 1106 | 61 | 62 | 63 | 122 |

### 6.2 hostcap-c steady (−5 мин) — primary

| Серия | n | p50 | p95 | p99 | max | S/P p99 |
|-------|--:|----:|----:|----:|----:|--------:|
| S_okx | 5216 | 29 | 31 | 33 | 130 | 1.00× |
| P_okx | 5206 | 29 | 31 | 33 | 130 | — |
| S_bybit | 8415 | 21 | 22 | 24.9 | 44 | 1.04× |
| P_bybit | 8404 | 20 | 21 | 24 | 46 | — |

Ping samples: OKX 6883 / Bybit 10744. Dual>500/1000: **0/16** мин. CSV: `output/hostcap_c/hostcap_c_summary.csv`.

### 6.3 OLD E2b partial (для сравнения; run мог ещё идти)

| Серия | n | p50 | p95 | p99 | max | S/P p99 |
|-------|--:|----:|----:|----:|----:|--------:|
| S_okx | 29384 | 31 | 33 | 37 | 51 | 1.06× |
| P_okx | 30210 | 31 | 33 | 35 | 689 | — |
| S_bybit | 41650 | 18 | 21 | 22 | 109 | 1.00× |
| P_bybit | 43337 | 18 | 19 | 22 | 59 | — |

Dual>500/1000: **0/86** мин.

---

## 7. Anti-scope / риски

- Не stop prod на новом хосте (уже active, PID `5276`).
- Не kill E2b PIDs на old.
- Не compaction / backup / retention / delete `/data/live`.
- Не правка ingest / parse / spread.
- **Риск coexistence:** full-N prod + N=1 shadow делили CPU/сеть — NEW не «тихий» fat host; при этом S всё равно ≈ P.
- **Риск clock regress:** если chrony упадёт — latency метрики снова съедут; мониторить `chronyc tracking` / `timedatectl`.
- **N=1 only:** не экстраполировать на full universe.

---

## 8. Следующий шаг

1. Опционально: финальный пересчёт E2b после `21:41:36Z` (ожидаемо те же порядки).
2. **H_host при полном N:** matched XRP из prod `/data/live` на NEW vs ping (или full-N shadow).
3. Review Critic / Orchestrator: не закрывать `gate #1` из hostcap-c; H1 остаётся ведущей для prod-хвостов.
