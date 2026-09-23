# 5-минутное окно тиков спреда

Примитивная research-визуализация **всех lean-тиков одной монеты** в одном окне (по умолчанию 5 минут). Не модель, не флор, не live.

Ноутбук: [`research/tick_window_5m.ipynb`](../tick_window_5m.ipynb).

## Pipeline block

- Трек: историческая модель / research visualization.
- Блок: чтение уже записанных lean-тиков (local cache **или** срез на VPS).
- Владелец: Model Simulator / research. Пути storage — только чтение.
- Не трогает: WebSocket ingest, парсинг бирж, расчёт спреда в collector, live routing.

## Откуда данные

| Среда | Путь | Что это |
|-------|------|---------|
| Mac, локальный кэш | `output/lean_ticks/spread_*.parquet` | Копия compacted окон, здесь до ~2026-08-27. **Не** бекап. |
| VPS, Sept dump | `/data/experiments/gear22_ticks_sept` | Эксперимент gear22, не durable SoT. |
| VPS, рабочий слой | `/data/compacted`, `/data/compacted/sent`, hive `/data/live` | Короткоживущий контур collector. |
| Durable backup | `backup1tb:spread-compacted` (rclone **на VPS**) | Канон тиков. На Mac rclone remote **не** настроен. |

Имена compacted: `spread_YYYYMMDDTHHMMSSZ_YYYYMMDDTHHMMSSZ.parquet` — одно ~5-минутное окно, **все монеты в одном файле**. Фильтр `base_coin` делается при чтении.

Схема lean (16 колонок): см. [`docs/storage-contract.md`](../../docs/storage-contract.md), [`docs/model-data-sources.md`](../../docs/model-data-sources.md).  
`spread_long` / `spread_short` **не лежат в файле** — считаются при чтении из L1.

## График и задержка сообщения

Одна фигура Plotly (~1500×1750), общая ось X = `event_dt` UTC (`event_local_ts_ms`):

1. `spread_long` + `spread_short` и поверх них три locked TW-p50:
   `p50_bar5m`, `p50_roll_1m`, `p50_roll_5m` (long и short). Формулы:
   [`docs/gear22-state-p50.md`](../../docs/gear22-state-p50.md). SMA / флор
   сюда не рисуем.
2. OKX bid
3. OKX ask
4. Bybit bid
5. Bybit ask

Наведение на bid/ask показывает **задержку той биржи, которая отправила эту книгу**:

```text
okx_latency_ms   = okx_local_recv_ts_ms − okx_ts_ms
bybit_latency_ms = bybit_local_recv_ts_ms − bybit_ts_ms
```

Та же формула, что в `research/lean_ticks_io` и gear22 viz. Не смешанная pair-delay и не `event_local_ts_ms − exchange_ts`.

Lean-строка — **снимок пары** (обе книги в одном ряду). Колонка `trigger` (`okx` | `bybit`) говорит, какая биржа породила этот ряд:

- `trigger` совпадает с venue точки → delay = send-to-receive **этого** сообщения;
- иначе delay = send-to-receive **последнего** сообщения той биржи (удерживаемая книга), не «возраст книги сейчас» (`calc_local_ts_ms − local_recv`).

## Как запускать

Из корня репо, interpreter = `venv` проекта:

```bash
cd /Users/mishatrubik/Desktop/spread
PYTHONPATH=. ./venv/bin/python -m jupyter notebook research/tick_window_5m.ipynb
```

В первой code-ячейке заполнить:

1. `COIN` — как в storage, например `BTC`, `SOL`, `KAITO`.
2. `WINDOW_START` — начало окна в **UTC** (`2026-09-01T00:00:00Z`). Naive = UTC.

Дальше: `WINDOW_MINUTES = 5`, при необходимости `SOURCE` и пути.

`SOURCE`:

- `auto` — если в `TICKS_ROOT` есть пересекающиеся `spread_*.parquet`, читать локально; иначе SSH.
- `local` — только Mac-кэш.
- `ssh` — всегда VPS: фильтр на сервере, на Mac приезжает только срез монеты.

Проверенный SSH (уже есть в репо/доках, без правок `~/.ssh`): `root@38.180.94.108`, python `/root/venv/bin/python`.

Проверка хелпера без ноутбука:

```bash
PYTHONPATH=. ./venv/bin/python -m unittest tests.test_tick_window_viz
```

## Что не доказано

- Что локальный `output/lean_ticks` равен `backup1tb:spread-compacted`.
- Что `/data/experiments/gear22_ticks_sept` — полная копия бекапа.
- Покрытие календаря: дыры не интерполируются; пустое окно = честный пропуск.
- Прибыльность, готовность live, корректность флора.

Не скачивать дерево бекапа на Mac. Не писать секреты в файлы. Не commit больших HTML/данных.
