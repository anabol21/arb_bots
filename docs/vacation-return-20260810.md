# Возврат с отпуска — 2026-08-10

> **Host migration (2026-08-10):** current production collector host is `root@38.180.94.108`. The IP below is the historical host for this report.

**Вердикт данных: GO WITH GAPS** — durable ticks на remote покрывают **полностью 6 августа** и **7 августа до ~16:45 UTC**; **8–10 августа на durable remote отсутствуют**.  
**Вердикт ops: CRITICAL** — диск VPS **100% full**, запись с ~05:35 UTC 10.08 деградирует; коллектор жив, но persistence ломается.

| Контекст | Где |
|----------|-----|
| Проверка | VPS `root@38.244.198.42`, UTC ~12:45–13:17; Mac download ~13:08–13:17 |
| Runtime | `spread-collector` → `app/screaner_b_o.py` |
| Runtime log | `/var/log/spread/runtime.log` (+ ротация `.1`…`.5.gz`) |
| Первая материализация | `/data/live`, `/data/bars` (локальный диск VPS) |
| Durable ticks | `backup1tb:spread-compacted` |
| Durable bars | `backup1tb:spread-bars` |
| Локальный кэш Mac | `output/vacation_return_20260810/{ticks,bars}/` |

Baseline уезда: `docs/pre-leave-health-20260806.md` (GO WITH CONDITIONS; bars backlog; OOM риск).

---

## 1. Что собрано за отпуск (durable remote)

### Ticks (`backup1tb:spread-compacted`)

| День (UTC, по имени файла) | Окна 5m | Размер | Статус |
|----------------------------|---------|--------|--------|
| 2026-08-05 (lean старт) | 146/288 с **11:50** | 1.26 GiB | Частичный день (не vacation core) |
| **2026-08-06** | **288/288** | **2.36 GiB** | **Полный день, lean** |
| **2026-08-07** | **202/288** до **16:45** | **1.73 GiB** | Обрыв после `…T164500Z` |
| 2026-08-08 | 0 | 0 | Нет на remote |
| 2026-08-09 | 0 | 0 | Нет на remote |
| 2026-08-10 | 0 | 0 | Нет на remote |

Remote всего: **902 объекта / 11.0 GiB** (было ~554 / 8.0 GiB на уезде).

Vacation lean скачан на Mac: **490/490 файлов, 4.08 GiB**, size-match 100%.

### Lean vs старый v1 canary (тот же remote)

| Эпоха | Пример файла | Строк | Колонок | Примечание |
|-------|--------------|-------|---------|------------|
| v1 canary | `spread_20260803T133500Z_…` (~46 MiB) | 1 000 000 | 25 | есть `spread_long/short`, latency/freshness |
| lean | `spread_20260806T120000Z_…` (~8 MiB) | 400 000 | 16 | book + ts; спреды **не** в файле |

Различие по размеру окна: v1 часто ≥20 MB/окно; lean типично ~6–11 MB.

### Bars (`backup1tb:spread-bars`)

| event_date | Файлов на remote | MiB |
|------------|------------------|-----|
| 2026-08-05 | 14 511 | 49.5 |
| 2026-08-06 | 188 | 0.64 |
| 2026-08-07 | 183 | 0.62 |
| 2026-08-08…10 | 0 | 0 |

Remote bars всего: **14 882 / 50.8 MiB** (было ~3.1k / 10.7 MiB). Catch-up **всё ещё застрял** около `base_coin=0G` / Aug-07; локально на VPS `/data/bars/bar_5m` ≈ **1.2 G / ~285k parquet**, `sent` почти пуст.

Скачано на Mac (только Aug 6–7): **371 файл / 1.4 MiB**. Схема lean bar: `bar_start_ts_ms, bar_end_ts_ms, base_coin, ref_exchange, volume`.

---

## 2. Инциденты и таймлайн (UTC)

### Collector OOM / restart (`NRestarts=9` на момент проверки)

| Когда | Событие |
|-------|---------|
| Aug 6 09:20 | oom-kill → restart (~как в pre-leave) |
| Aug 6 15:30 | oom-kill → restart |
| Aug 7 00:11 | oom-kill → restart |
| Aug 7 10:56 | oom-kill → restart |
| Aug 8 01:41 | oom-kill → restart |
| Aug 8 04:07 | oom-kill → restart |
| Aug 8 23:48 | oom-kill → restart |
| Aug 9 04:44 | oom-kill → restart |
| Aug 10 03:35 | oom-kill → restart (journal); текущий uptime с 03:35:31 |

Каждый restart = короткий gap ingest (обычно секунды–минуты). Heartbeat gaps ≥5m **за Aug 6–9 почти нет**; крупные gaps — **Aug 10** при ENOSPC (до ~87 мин по логу).

### Compaction / tick backup

- Последние успешные `compaction_complete` в логе: **Aug 9 04:45–04:46** для окон **`20260807T1625…1645`**.
- После этого remote ticks **не росли** по Aug 8–10.
- Tick backup timer жив, но `backlog_files_count=0` — в `/data/compacted` нечего грузить (compaction не производит новые final), при этом **`/data/live` ≈ 20 G / ~797k parquet** не уезжают в compacted.
- На VPS остаются локальные live-партиции Aug 7–10 (пример BTC: Aug7 16M … Aug10 15M) — **не durable**, пока не compacted+backup.

### Disk full / ENOSPC (критично сейчас)

| Когда | Событие |
|-------|---------|
| Aug 10 ~05:35 | Первые `No space left on device` (bars + live); `spool_write_failed` / `quarantine_write_failed` |
| Aug 10 05:35→сейчас | `published_rows` долго залипал; `failures` растёт (347+); heartbeat иногда рвётся |
| Aug 10 ~12:48 | `df`: **0 avail**, `/` 100%; `/data/live` 20G; `/var/log` 2.6G; journal ~0.8G |
| Aug 10 ~12:54 | Safe: `journalctl --vacuum-size=200M` → освобождено ~615M archived journals (как в pre-leave) |
| После vacuum | Кратко снова пошли publish (published_rows сдвинулся с 8.8M→~10.8M), но **df всё ещё 100% / 0 Avail** для non-root |

Bars backup на Aug 10 05:35 упал на `download_verify` (`preallocate: file too big for remaining disk space`) при backlog summary ранее ~**279k files / ~954 MB**.

### Прочее

- `mount_failure`: **0** в runtime.log.
- Swap: **0**; RAM ~1.9G; OOM storm по syslog Aug 7–9 в основном бил **compactor** (сотни–тысячи oom-событий/день), не только collector.
- Timers `spread-compactor` / `spread-backup-transfer` / `spread-bars-backup-transfer`: active, но бесполезны без свободного места и без новых compacted.

---

## 3. Локальный кэш на Mac

```
output/vacation_return_20260810/
  ticks/     # 490 lean compacted parquet, 4.08 GiB (Aug6 full + Aug7→16:45)
  bars/      # hive bar_5m, 371 files, 1.4 MiB (Aug6–7 remote only)
  _meta/     # lsl, progress log, samples (v1+lean)
```

Проверка: **ok=490/490**, size == rclone lsl.  
Mac disk: было ~252 GiB free → после ~248 GiB (окно ≪ 50 GiB, без предупреждения).

**Не скачивалось:** весь исторический v1 canary Aug 3–4; Aug 5 partial; Aug 8–10 (нет на remote).

---

## 4. GO / NO-GO

| Вопрос | Вердикт |
|--------|---------|
| Исследовать lean ticks **2026-08-06** | **GO** |
| Исследовать lean ticks **2026-08-07 до 16:45 UTC** | **GO** (неполный день) |
| Durable ticks **Aug 8–10** | **NO-GO** (только рискованный VPS `/data/live`) |
| Bars vacation как полный ряд | **NO-GO** (remote крохи; локальный backlog не durable) |
| Ops / продолжение сбора сегодня | **NO-GO без emergency disk reclaim** |

---

## 5. Как открыть в model notebooks

Текущий `model.ipynb` ждёт hive `output/spreads_parquet_by_coins/base_coin=…/event_date=…` и поля вроде `spread_long` (v1).  
Vacation ticks — **flat compacted lean** (мульти-монета на 5m окно, **без** готовых spread-колонок).

Минимальный путь для исследования:

```python
from pathlib import Path
import pyarrow.parquet as pq
import pandas as pd

TICK_DIR = Path("output/vacation_return_20260810/ticks")
files = sorted(TICK_DIR.glob("spread_20260806*.parquet"))  # полный день
# один файл = одно 5m окно, много монет
t = pq.read_table(files[0])
df = t.to_pandas()
# lean cols: event_local_ts_ms, base_coin, trigger, *_ts_ms, book prices/sizes
coin = df[df.base_coin == "BTC"].sort_values("event_local_ts_ms")
# spread нужно считать из стакана (okx/bybit bid/ask), как в lean contract
```

Для гира модели: либо конвертер lean-compacted → per-coin hive + derived spreads, либо отдельная ячейка загрузки; **не** подставлять путь vacation ticks напрямую в `PARQUET_ROOT` без адаптации схемы.

Bars: `output/vacation_return_20260810/bars/bar_5m/base_coin=*/event_date=2026-08-0[67]/` — мало покрыты монеты (remote catch-up).

---

## 6. Срочный next step (нужно явное одобрение)

Без удаления данных коллектор снова упрётся в ENOSPC.

Безопасные кандидаты (с подтверждением):

1. **Emergency disk reclaim** на VPS: vacuum/ротация логов; удаление **уже забэкапленных** `archived` / старых live после сверки с remote; **не** трогать collector stop без нужды.
2. Дожать compaction Aug 7 16:50 → Aug 10 из `/data/live` → remote (иначе 2.5+ дня только локально).
3. Отдельно: bars throughput / verify-on-full-disk (сейчас verify убивает drain при ENOSPC).

Коллектор **не останавливали**. Remote **не разрушали**. Commit не делался.
