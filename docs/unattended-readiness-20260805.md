# Unattended readiness — 2026-08-05

> **Host migration (2026-08-10):** current production collector host is `root@38.180.94.108`. The IP below is the historical host for this report.

**Вердикт: YES WITH CONDITIONS** — уехать на несколько дней можно, но это не «забыл и всё само идеально». Сборщик сейчас запущен; локальный диск ограничен; remote backup растёт без потолка.

| Контекст | Где |
|----------|-----|
| Код правится | локальный репо → rsync `/root/spread_staging` |
| Исполнение (исторически 2026-08-05) | VPS `root@38.244.198.42` |
| Current production host (2026-08-10+) | VPS `root@38.180.94.108` |
| Runtime log | `/var/log/spread/runtime.log` |
| Первая материализация | `/data/live`, `/data/bars` (локальный диск, не mount) |
| Durable backup | `backup1tb:spread-compacted` (rclone SFTP) |

---

## 1. Состояние на момент проверки (UTC 2026-08-05 ~11:55)

| Компонент | Было | Стало после действий |
|-----------|------|----------------------|
| `spread-collector.service` | inactive, **disabled** | **active + enabled**, lean+bars |
| Lean flags | unit уже имел `SPREAD_LEAN_SCHEMA=1`, `SPREAD_COLLECT_BARS=1` | подтверждено в логе: `schema_mode=lean`, `collect_bars=true`, 337 pairs |
| Compactor / backup timers | active + enabled | без изменений (каждые 5 мин) |
| Dual ping `ping_okx_bybit_2h.py` | pid 351542 ещё крутился | **остановлен** |
| Disk `/` | 27% used, **~21G free** | ~26%, ~21G free |
| Remote backup | 266 objects / **5.661 GiB** | растёт forever (ожидаемо) |
| FNF / offloaded fix | задеплоен; `compaction_artifact_offloaded` INFO; retention удаляет archive | подтверждено (напр. `removed_files=1280`) |
| Logrotate | отсутствовал; `runtime.log` 183M | `/etc/logrotate.d/spread` + разовый rotate + gzip старых `.1` |

Smoke после старта (после фикса `LimitNOFILE`), ~T+10 мин (12:02 UTC):

- ticks `event_date=2026-08-05`: **~2685** parquet / **700k** rows; `failures=0`, `spool=0`;
- bars: **336** parquet / `bar_published_rows=500` после границы `:00`;
- RSS ~530 MiB; `NRestarts=0`; ERROR в логе = старые EMFILE/SSL до `LimitNOFILE` (после рестарта — чисто).

---

## 2. Reconnect / resilience (код + systemd)

| Слой | Поведение | Оценка |
|------|-----------|--------|
| OKX / Bybit / candle5m listeners | `while True` + `except` → `sleep(10)` → reconnect | OK для кратких обрывов; **нет exponential backoff** |
| Heartbeat | каждые 30s (метрики publisher/spool) | OK |
| SIGTERM shutdown | flush buffer → spool on backpressure → publisher drain | OK (проверено canary) |
| Spool recovery | `SpoolRecoveryWorker` на старте | OK |
| Primary storage hard-fail | cancel main → spool → exit non-zero | systemd поднимет снова |
| systemd | было `Restart=on-failure` | сейчас **`Restart=always`**, `RestartSec=5`, `StartLimitBurst=20` / 300s |
| FD limit | default 1024 ломал lean+bars (~337×3 WS) | **`LimitNOFILE=65535`** |

---

## 3. Disk lifecycle (локальный `/`)

| Путь | Политика | Bounded? |
|------|----------|----------|
| `/data/live` active batches | → compact → archive | да, при работающем timer |
| `/data/live/archived` | retention **12h** (ужесточено с 24h для unattended) | да, если compact manifests complete |
| `/data/compacted` | уходит в backup | backlog ≈0 при здоровом rclone |
| `/data/compacted/sent` | retention **12h** после `sent_at` | да |
| `/data/bars` | backup hive → `backup1tb:spread-bars`, local `sent/` + retention **12h** | да, при работающем bars timer (см. throughput residual в `docs/bars-backup-20260805.md`) |
| `/var/log/spread/*` | logrotate daily/size 50M, 7 copies, copytruncate | да |
| Remote `backup1tb:` | только append | **не bounded** — это нормально, пока есть место на remote |

Canary v1: archive ~**1.3 GiB/h**. Lean меньше, но на 30G диске запас всё ещё главный риск. При 12h archive + 12h sent локальный steady-state должен удерживаться лучше, чем при 24h; доказанного multi-day lean soak на full universe **нет**.

---

## 4. Что сделано прямо сейчас (минимум для выживания)

1. Остановлен dual ping.
2. Включён и запущен `spread-collector` (lean ticks + OKX bars).
3. `Restart=always` + start-limit; `LimitNOFILE=65535`.
4. Compactor `--retention-hours 12` (было 24).
5. Установлен logrotate для `/var/log/spread`; старые логи сжаты.
6. Timers compactor/backup оставлены enabled/active.

Локальные артефакты в репо: `deploy/systemd/spread-collector.service`, `spread-compactor.service`, `deploy/logrotate/spread`.

---

## 5. Autonomous vs нужно человеку

**Само чинится / крутится:**

- WS reconnect (фиксированный 10s);
- systemd restart collector при падении;
- compact каждые 5m + archive retention 12h;
- backup transfer каждые 5m + sent retention 12h;
- logrotate (ежедневный cron logrotate).

**Не само / нужен человек или внешний мониторинг:**

- диск `/` → 0 (OOM disk) — collector/spool начнут деградировать;
- remote backup full — transfer встанет, `compacted/`/`sent/` раздуются локально;
- WS ban / длительный SSL/API outage — gaps в данных, процесс может «жить» с ошибками;
- RAM 1.9G без swap — OOM kill возможен при пике;
- bars без lifecycle — на недели/месяцы уже не «бесплатно»;
- нет алерта на телефон из этой сессии.

---

## 6. Residual risks (честно)

1. **Диск** — главный cliff; multi-day lean full-universe не доказан wall-clock.
2. **Backup remote заполняется forever** — ожидаемо; при заполнении пострадает локальный диск.
3. **Rclone down hours** — local compacted backlog растёт, пока remote не оживёт.
4. **Нет exponential backoff** — thrash при массовом disconnect.
5. **Memory / FD** — bars утроили WS; LimitNOFILE закрыл EMFILE, RAM всё ещё тесная.
6. **Старые v1 day partitions** (Aug 3–4) ещё под `/data/live`; сегодня lean пишет `event_date=2026-08-05` (не смешиваем день).
7. Phone-home / ops_alerts не настроены как pager.

---

## 7. Success criteria на время отсутствия

Считать «выжило», если по возвращении:

1. `systemctl is-active spread-collector` → active, `NRestarts` не в сотнях;
2. `df -h /` → free **> 3G**;
3. свежие heartbeat + parquet за последние часы;
4. backup timer без хронического backlog (`backup_summary backlog_files_count≈0`);
5. нет нового шторма `compaction_alert` (допустимы INFO `compaction_artifact_offloaded`).

---

## 8. Recommended next step (по возвращении)

1. `df -h /`; `systemctl status spread-collector`; tail heartbeat.
2. Сверить remote size vs ожидание (~сотни MB–GB lean/день).
3. Если диск давит — ещё ужать archive retention или чистить старые canary dirs **только осознанно**.
4. Отдельный lifecycle для `/data/bars` (compact/backup или mtime prune), если bars остаются в prod надолго.
