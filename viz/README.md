# Spread visualization (Mac + backup cache)

Локальный сервис для коллег: React UI + FastAPI.  
**Source of truth:** `backup1tb:spread-compacted`.  
**Кэш на Mac:** `output/lean_ticks`.  
**Не деплоить на VPS** рядом со сборщиком / trade bot — UI не должен читать `/data/live`.

## Быстрый старт

Из корня репо (нужен `venv` с fastapi / uvicorn / duckdb):

```bash
# один раз: зависимости API
./venv/bin/python -m pip install fastapi 'uvicorn[standard]' duckdb

# один раз: фронт (если нет viz/web/dist)
cd viz/web && npm install && npm run build && cd ../..

# каталог файлов + сервер на LAN
./venv/bin/python -m viz --rebuild-catalog --host 0.0.0.0 --port 8787
```

Открыть:

- на Mac: http://127.0.0.1:8787/
- коллеги в LAN/VPN: http://&lt;ip-мака&gt;:8787/

API:

- `GET /api/meta`
- `GET /api/coins`
- `GET /api/coverage`
- `GET /api/ticks?coin=HOME&start=2026-08-18T00:00:00Z&end=2026-08-18T00:15:00Z`
- `POST /api/catalog/rebuild`

Окно с `n > 300000` тиков → **HTTP 400** (без silent downsample).

Дополнительно:

- страница монеты: ←/→ внутри списка (крипто / не крипто), клавиши стрелок;
- Bybit mid — отдельный график ниже спреда;
- overview всего покрытия (прореженно ≤8000) + p50/p95/p99 по всем тикам;
- главная: таблица статов как в `index.html` после сборки кэша.

**Кэш (не гонять полный скан каждый раз):**

```bash
# один раз после полной сборки (или уже есть overview_summary.json):
./venv/bin/python -m viz.build_overview --seed-only

# дальше — только НОВЫЕ parquet после sync (секунды–минуты, не сутки):
./venv/bin/python -m viz.build_overview

# полный пересчёт всех файлов (редко, часы):
./venv/bin/python -m viz.build_overview --full
```

Страница монеты: первый заход может просканировать покрытие и записать
`viz/data/overview_inc/coin_pages/{COIN}.json`; следующие открытия — с диска.

```bash
# долгий один проход → viz/data/overview_summary.json  (устарело как default)
# ./venv/bin/python -m viz.build_overview --legacy-full
```

## Sync с backup (отдельная команда)

Не вызывается из UI. Не трогает `/data/live`.

```bash
# dry-run: сколько missing
./venv/bin/python -m viz.sync_from_backup --dry-run

# инкремент + пересобрать DuckDB-каталог
./venv/bin/python -m viz.sync_from_backup --rebuild-catalog
```

Поведение:

1. Если на Mac есть `rclone` и remote `backup1tb` → прямой `rclone copy --files-from`.
2. Иначе VPS-hop: `nice -n 19 ionice -c3 rclone` → `/root/mac_lean_pull` → `rsync` на Mac → cleanup staging.  
   Transfers по умолчанию **4** (низкая нагрузка).

Флаг `--force-vps-hop` принудительно включает hop через VPS.

## Dev (hot reload UI)

Терминал 1:

```bash
./venv/bin/python -m viz --host 127.0.0.1 --port 8787
```

Терминал 2:

```bash
cd viz/web && npm run dev
```

Vite: http://127.0.0.1:5173/ (прокси `/api` → 8787).

## Изоляция VPS (обязательно)

| Делать | Не делать |
|--------|-----------|
| Читать только локальный `output/lean_ticks` | Ставить viz systemd на collector VPS |
| Sync one-shot / редкий cron с nice/ionice | On-demand read `/data/live` или `/data/compacted` из UI |
| Staging `/root/mac_lean_pull` | Держать тяжёлый staging рядом с live без cleanup |

## Структура

```
viz/
  app.py              FastAPI
  catalog.py          DuckDB индекс имён parquet
  ticks.py            all-ticks load (lean_ticks_io)
  sync_from_backup.py инкремент с backup
  config.py
  web/                React (Vite) + dist/
```
