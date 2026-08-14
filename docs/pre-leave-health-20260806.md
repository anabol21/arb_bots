# Pre-leave health — 2026-08-06

> **Host migration (2026-08-10):** current production collector host is `root@38.180.94.108`. The IP below is the historical host for this report.

**Вердикт: GO WITH CONDITIONS** — уезжать на 3 дня можно: ticks-коллектор, compaction и tick-backup здоровы; bars remote **не догоняет** backlog; сегодня уже был **OOM-kill** коллектора (systemd поднял снова).

| Контекст | Где |
|----------|-----|
| Проверка | VPS `root@38.244.198.42`, UTC ~12:03–12:10 |
| Runtime | `spread-collector` → `app/screaner_b_o.py` |
| Runtime log | `/var/log/spread/runtime.log` |
| Первая материализация | `/data/live`, `/data/bars` (локальный диск) |
| Durable ticks | `backup1tb:spread-compacted` |
| Durable bars | `backup1tb:spread-bars` (catch-up, отстаёт) |

---

## Вердикт по блокам

| Блок | Статус | Комментарий |
|------|--------|-------------|
| Collector | OK с оговоркой | active+enabled, lean+bars, `failures=0`, spool=0; **NRestarts=1** из‑за OOM 09:20 UTC |
| Disk | OK после vacuum | 45% / **16G free**; archive retention 12h работает |
| Compactor + tick backup | OK | timers active; backlog ≈1 файл; remote растёт (сегодняшние 5m окна) |
| Bars backup | CRITICAL (durability) | oneshot 18h+, ~60k pending vs ~3.1k remote; диск не давит (~265M) |
| Retention / FNF | OK | `archive_retention_complete` каждые ~5m; `compaction_artifact_offloaded` INFO, не alert-storm |
| Ops alerts | OK | `validation/ops_alerts.py --once` → `ops_alert_ok`, exit 0 |

---

## Ключевые числа (UTC 2026-08-06 ~12:08)

| Метрика | Значение |
|---------|----------|
| Collector | active, enabled, uptime ~2h48m с 09:20, RSS ~700 MiB |
| Env | `SPREAD_LEAN_SCHEMA=1`, `SPREAD_COLLECT_BARS=1` |
| Heartbeat | pairs=337, schema_mode=lean, collect_bars=true, failures=0 |
| published_rows / files | 14 000 000 / 47 040 (растёт) |
| bar_published_rows | 11 000 |
| spool / failed_batches | 0 / empty log |
| Disk `/` | **16G free** (45%); до vacuum было ~14–15G free |
| `/data/live` | 3.7G (active ~2.7k parquet) |
| `/data/live/archived` | 3.6G (~200k files, oldest ~12h) |
| `/data/compacted` pending | ~1 файл / ~7.6 MB backlog |
| `/data/compacted/sent` | 1.2G (~145 files, oldest ~12h) |
| `/data/bars` pending | **~60.6k** parquet / ~246M |
| `/data/bars/sent` | ~3.15k / 15M |
| `/data/spool` | empty |
| Remote ticks | **554 objects / 8.004 GiB**; newest ~`…T114500Z…` + in-flight |
| Remote bars | **3143 objects / 10.7 MiB**; ещё Aug-05 catch-up (сейчас около BNB) |
| RAM | 1.9G, **swap=0**, MemAvailable ~760 MiB |
| Timers | compactor + tick backup: NEXT каждые 5m; bars timer Trigger=n/a пока oneshot active (ожидаемо) |

---

## Что сделано в этой проверке (safe)

1. `journalctl --vacuum-size=300M` → освобождено **~1.2G** journal.
2. Удалены явные junk-логи остановленных ping/lean-soak экспериментов в `/var/log/spread` (~десятки MB).
3. Collector **не** трогали; bars oneshot **не** убивали (идёт медленный drain).

---

## Условия / риски на 3 дня

1. **OOM** — сегодня 09:20 UTC `Failed with result 'oom-kill'`; без swap повтор возможен. systemd `Restart=always` поднимает, но будет gap в данных.
2. **Bars remote** — ~27s/файл (download_verify); drain ≪ ingest → backlog будет расти; локально bars остаются, remote отстаёт сильно. Для ticks durable path OK.
3. **Диск** — при работающем retention 12h запас ~16G выглядит достаточным на 3 дня; cliff если retention/compactor встанет или remote backup застопорит `sent/`.
4. **WS «no close frame»** — частые ERROR с reconnect; не валят процесс, возможны мелкие gaps.
5. Нет phone pager — только ручной SSH / `ops_alerts`.

---

## Success criteria по возвращении

1. `systemctl is-active spread-collector` → active; NRestarts не в десятках+.
2. `df -h /` → free **> 3G**.
3. Свежий heartbeat, `failures=0`, свежие `/data/live/.../event_date=сегодня`.
4. Tick `backup_summary backlog_files_count` ≈ 0–few; remote compacted mtime свежий.
5. Bars: ожидать всё ещё большой pending — не регрессия «сломалось», а известный throughput debt.

---

## Emergency SSH (с телефона)

```bash
ssh root@38.180.94.108 'systemctl is-active spread-collector; df -h /; free -h | head -2; grep heartbeat /var/log/spread/runtime.log | tail -1'
```

```bash
ssh root@38.180.94.108 'systemctl list-timers "spread-*" --no-pager; tail -3 /var/log/spread/backup-transfer.log'
```

```bash
ssh root@38.180.94.108 'cd /root/spread_staging && /root/venv/bin/python validation/ops_alerts.py --once'
```

Если collector dead после OOM и не поднялся:

```bash
ssh root@38.180.94.108 'systemctl start spread-collector && systemctl status spread-collector --no-pager | head -20'
```

**Не делать** без необходимости: `systemctl stop spread-collector`, удаление `/data/live` или `/data/bars`, umount, правки rclone credentials.
