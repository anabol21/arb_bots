# Контракт Hyperliquid L1

Отдельный набор данных. Это не тело `lean` и не v1 из [`storage-contract.md`](storage-contract.md). В parquet нет колонок спреда, OKX и Bybit.

Спред с Bybit или OKX считается позже, при чтении, стыковкой по `base_coin` и времени с тиками `/data/live`. Этот контур туда не пишет.

## Кто пишет и кто читает

| Роль | Где |
|------|-----|
| Производитель | `python -m app.hl` (`app/hl/`). Не `app/screaner_b_o.py`. |
| Запись | существующий `ParquetPublisher` / `DurableSpool`, `schema_mode=hl_l1` |
| Корень первой записи | `/data/live-hl` (`HL_PARQUET_ROOT`) |
| Spool при сбое записи | `/data/spool-hl` (`HL_SPOOL_ROOT`). Не `/data/spool` и не `/data/spool-next`. |
| Потребитель | офлайн-стыковка позже. Модель этот корень не читает. |

Совместимость: **новый набор**. Старых файлов нет. Читатель lean/v1 этот корень не открывает. Откат: остановить процесс. Деревья D не меняются.

`SPREAD_LEAN_SCHEMA` этот режим не включает.

## Раскладка

```text
/data/live-hl/base_coin=<COIN>/event_date=<YYYY-MM-DD>/batch_*.parquet
```

`event_date` — календарный день UTC от `event_local_ts_ms`. В теле колонки нет. `base_coin` есть и в пути, и в теле.

## Колонки тела

Порядок как в `HL_L1_BODY_COLS`. Временные метки — int64, миллисекунды. Цены и размеры — float64.

| Колонка | Смысл |
|---------|--------|
| `event_local_ts_ms` | локальные часы в момент разбора кадра. Слушатель ставит то же значение, что и `hl_local_recv_ts_ms`. |
| `base_coin` | имя монеты Hyperliquid. Точное совпадение с `take=yes`, без подмены алиасов. |
| `hl_local_recv_ts_ms` | локальные часы приёма кадра WS. |
| `hl_ts_ms` | `data.time` кадра `bbo`. |
| `hl_bid_price` | `bbo[0].px` |
| `hl_bid_size` | `bbo[0].sz`, размер в монете, как прислала биржа |
| `hl_ask_price` | `bbo[1].px` |
| `hl_ask_size` | `bbo[1].sz` |

Обязательны все восемь. Пустая сторона книги, нечисло или неположительная цена не попадают в parquet: запись отклоняется и уходит в карантин spool. Нулевая цена не подставляется.

Не пишутся: `spread_long`, `spread_short`, `trigger`, колонки OKX/Bybit, `event_dt`, `event_date`.

## Монеты

`load_take_yes_pairs` по `bybit_okx_universe.csv`, затем пересечение с перпами Hyperliquid (`POST https://api.hyperliquid.xyz/info`, тело `{"type":"meta"}`). В подписку попадает только точное имя. `kPEPE` не заменяет `1000PEPE`. Несовпавшие имена пишутся в лог (`hl_universe_unmatched`, `policy=exact_name_only`).

Один сокет `wss://api.hyperliquid.xyz/ws`, подписка `bbo` на каждую совпавшую монету.

Сбой публикации не блокирует приём: кадр кладётся в очередь издателя. Очередь и буфер ограничены. Переполнение буфера увеличивает счётчик отбрасывания и пишется в лог.

## Что не считается долговечным

Локальный файл в `/data/live-hl` — первая запись на диске процесса, не удалённая копия. Spool — локальный запас при сбое этой записи, не бэкап.

Уплотнителя и таймера бэкапа для этого контура в этом шаге нет. `spread-compactor` и префикс `spread-compacted` сюда не направлять.

## Поздний гейт (не этот шаг)

Только после SSH и отдельного явного разрешения:

1. Выкладка в свой каталог на хосте. `systemctl enable` до этого не делать. Шаблон `deploy/systemd/spread-hl-l1.service` в git службу не включает.
2. Несколько суток записи в `/data/live-hl`.
3. Свой уплотнитель и свой таймер. Не `spread-compactor` и не `/data/compacted`.
4. Выгрузка префиксом rclone `spread-hl` (шаблон `deploy/systemd/spread-hl-backup-transfer.service`, без `[Install]` и без таймера). Не `spread-compacted`. Шаблон вызывает тот же `backup_transfer`, что и тики: `--compacted-dir /data/hl-compacted`, `--remote-path spread-hl`. Пока уплотнителя нет, этот каталог не создаётся и шаблон не включают. Раскладка файлов уплотнителя входит в тот же гейт.
5. Чтение уже уплотнённого parquet с remote.

Пока шагов 2–5 нет, удалённая долговечность не заявляется. Локальная проверка отбора и одного parquet этого не доказывает.
