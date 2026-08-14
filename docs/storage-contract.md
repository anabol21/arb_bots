# Контракт хранения спредов (трек сбора)

Единый список колонок тела parquet и раскладки партиций.

Источники правды в коде:

- тики v1: [`app/schema/spread_event.py`](../app/schema/spread_event.py)
- тики lean + бары `bar_5m`: [`app/schema/lean_event.py`](../app/schema/lean_event.py)
- hive-раскладка: [`app/schema/parquet_layout.py`](../app/schema/parquet_layout.py)

Взгляд модели (гир 1–2): [`docs/data-format-model.md`](data-format-model.md).  
Gap ingest ↔ модель: [`docs/data-format-ingest-gap.md`](data-format-ingest-gap.md).  
Операционка lean: [`docs/local-lean-collector.md`](local-lean-collector.md).

Рантайм-вход: `app/screaner_b_o.py` → нормализация/запись: `app/storage/writer.py`.

Флаги режима (default **OFF** → v1; production accumulation → lean+bars):

| Env | Эффект |
|-----|--------|
| `SPREAD_LEAN_SCHEMA=1` | тиковый body = lean |
| `SPREAD_COLLECT_BARS=1` | слой OKX `candle5m` → `bar_5m` |

---

## Путь на диске — тики

```text
<SPREADS_ROOT>/base_coin=<COIN>/event_date=<YYYY-MM-DD>/<batch_or_part>.parquet
```

- `base_coin`, `event_date` — hive-партиции в пути.
- В теле lean/`bar_5m` колонка `base_coin` дублируется для удобства чтения; `event_date` в body **нет** (writer отбрасывает).

Default roots: `SPREAD_PARQUET_ROOT=/data/live`, `SPREAD_BARS_ROOT=/data/bars`.

---

## Body-колонки — полный контракт v1 (canary / legacy)

Порядок как в `SPREAD_EVENT_BODY_COLS`:

1. `event_dt`
2. `event_local_ts_ms`
3. `base_coin`
4. `trigger`
5. `spread_long`
6. `spread_short`
7. `okx_latency_ms`
8. `bybit_latency_ms`
9. `okx_freshness_ms`
10. `bybit_freshness_ms`
11. `max_freshness_ms`
12. `max_latency_ms`
13. `calc_local_ts_ms`
14. `okx_local_recv_ts_ms`
15. `okx_ts_ms`
16. `bybit_local_recv_ts_ms`
17. `bybit_ts_ms`
18. `okx_bid_price`
19. `okx_bid_size`
20. `okx_ask_price`
21. `okx_ask_size`
22. `bybit_bid_price`
23. `bybit_bid_size`
24. `bybit_ask_price`
25. `bybit_ask_size`

Производные при нормализации v1:

- `okx_freshness_ms` = `calc_local_ts_ms − okx_local_recv_ts_ms` (аналогично Bybit)
- `event_local_ts_ms` = recv триггера (`okx` или `bybit` по `trigger`)
- `max_freshness_ms` / `max_latency_ms` = max по двум биржам

L1 book: публичный лучший bid/ask (цена + размер). Суффикс объёма — `_size`.

---

## Body-колонки — lean ticks (production target)

Версия контракта: **lean** (включается `SPREAD_LEAN_SCHEMA=1`).  
Порядок как в `LEAN_TICK_BODY_COLS`. Все временные метки — **int64 ms**.

1. `event_local_ts_ms`
2. `base_coin`
3. `trigger`
4. `calc_local_ts_ms`
5. `okx_local_recv_ts_ms`
6. `okx_ts_ms`
7. `bybit_local_recv_ts_ms`
8. `bybit_ts_ms`
9. `okx_bid_price`
10. `okx_bid_size`
11. `okx_ask_price`
12. `okx_ask_size`
13. `bybit_bid_price`
14. `bybit_bid_size`
15. `bybit_ask_price`
16. `bybit_ask_size`

**Не пишутся** (считать при чтении):

- `spread_long` / `spread_short` — из L1  
  - long = `(bybit_bid − okx_ask) / bybit_bid × 100`  
  - short = `(okx_bid − bybit_ask) / okx_bid × 100`
- `*_latency_ms`, `*_freshness_ms`, `max_*`, `event_dt`

Единицы lot/tick/min-size — **не** в parquet; join из [`bybit_okx_universe.csv`](../bybit_okx_universe.csv) по `base_coin`.

Не смешивать lean и v1 в одной дневной партиции без dual-read ридера.

---

## Body-колонки — `bar_5m` v0

Отдельный dataset, не смешивать с тиковыми batch:

```text
<BARS_ROOT>/bar_5m/base_coin=<COIN>/event_date=<YYYY-MM-DD>/….parquet
```

Порядок как в `LEAN_BAR_5M_BODY_COLS`:

1. `bar_start_ts_ms` — начало окна (включительно), int64 ms
2. `bar_end_ts_ms` — `bar_start_ts_ms + 300_000` (исключительно)
3. `base_coin`
4. `ref_exchange` — канон модели: `okx`
5. `volume` — объём закрытой свечи

### Семантика `volume` (зафиксировано)

| Биржа | WS | Канал | Persist when | Поле | Единица |
|-------|-----|-------|--------------|------|---------|
| OKX (канон) | business `wss://ws.okx.com:8443/ws/v5/business` | `candle5m` | `confirm == "1"` | `volCcy` | base coin (SWAP) |
| Bybit (опц.) | linear public | `kline.5.{symbol}` | `confirm == true` | `volume` | base coin |

Не включать: OHLC, amplitude, spreads, `n_updates`, unit-колонки.

---

## Durable `bar_5m` v2 — compacted layout

**Producer:** frozen collector продолжает писать source-batch в
`/data/bars/bar_5m`. **Compactor:** отдельный one-shot process. **Consumers:**
backup и историческая модель (gear 1.5+). Это изменение layout, а не полей
свечи: parquet body остаётся ровно `LEAN_BAR_5M_BODY_COLS` из пяти колонок
выше, с теми же типами, UTC и семантикой `volume`.

```text
# mutable local source; collector only
/data/bars/bar_5m/base_coin=<COIN>/event_date=<UTC-date>/batch_*.parquet

# compacted v2 publication; one COIN × closed one-hour UTC window
/data/bars_compacted_v2/bar_5m/base_coin=<COIN>/event_date=<UTC-date>/
  bar_5m_<YYYYMMDDTHHMMSSZ>_<YYYYMMDDTHHMMSSZ>_inputset=<16-hex>.parquet
```

- Hive keys остаются `base_coin`, `event_date`; они в пути, `event_date` не
  добавляется в body. `base_coin` остаётся body-колонкой.
- Окно имеет `[window_start, window_end)` в UTC, по умолчанию 3600 s. Оно
  eligible только после `window_end + grace`, а source batch должен быть
  неизменным до этой же границы; текущий/open час не compact-ится. Первый
  manifest замораживает точный список source path/bytes/rows/SHA-256. Поздний
  batch для уже замороженного часа не добавляется и не создаёт второй
  model-visible output: он остаётся на source root и получает quarantine
  record/alert. Batch, пересекающий границу окна, пропускается.
- Публикация: `.inprogress` → fsync/read-back (rows + schema) → atomic rename
  в final. Имя final включает digest замороженного input set, поэтому remote
  identity не коллидирует между разными наборами input. Sidecar manifest
  содержит версию layout, source paths/sizes/SHA-256, row count, output
  SHA-256/bytes и lifecycle status. Existing final принимается только при
  exact checksum match; другой checksum переводится в quarantine, без overwrite.
- После локальной публикации source переносится только в локальный archive.
  Его retention разрешён лишь когда отдельный transfer manifest подтвердил
  final object на `backup1tb:spread-bars-compacted-v2` (`sent` после
  temporary→final, remote-size и SHA verification). Локальная compaction сама
  по себе **не** является durable remote boundary.
- Временные/partial `.inprogress`, `.tmp` и incomplete manifests не являются
  модельным входом и не передаются как final.

### Совместимость и миграция

Это additive layout version **2**, обратимо совместимый на уровне parquet:
existing model reader, который рекурсивно читает hive `*.parquet`, получает те
же обязательные bar columns. Reader должен явно выбрать один root:
legacy source `/data/bars/bar_5m` или durable compacted v2
`/data/bars_compacted_v2/bar_5m`; нельзя читать оба одновременно, иначе будут
дубликаты. Legacy remote `backup1tb:spread-bars` остаётся историческим,
disabled и не drain/delete без отдельного approval. Ранее созданные v1
`/data/bars_compacted` и `backup1tb:spread-bars-compacted` остаются
read-only: v2 не выполняет их миграцию, удаление, перезапись или bulk-move.
Новые compacted objects идут только в
`/data/bars_compacted_v2/bar_5m` и `backup1tb:spread-bars-compacted-v2`.

---

## Вне контракта

- Приватные каналы (latency ордера, аккаунт).
- L2+, trades tape, funding, OI.
- Переименование `_size` → `_quantity`.

---

## Legacy

Файлы без 8 book-колонок допустимы как исторический хвост, но **не** как целевой продакшен-формат.  
Проверка бэкапа v1 canary: [`validation/check_backup_validity.py`](../validation/check_backup_validity.py) ожидает полный `EXPECTED_BODY_COLS` (= body v1 выше).

---

## Версии и совместимость

| Версия | Когда | Статус |
|--------|-------|--------|
| **v1** | canary / флаги off | заморожен; полный tick body |
| **lean** | `SPREAD_LEAN_SCHEMA=1` | production target для накопления |
| **bar_5m** v0 | `SPREAD_COLLECT_BARS=1` | additive слой; отсутствие не ломает тики |
| **bar_5m compacted** v2 | отдельный compactor + transfer | durable layout; body = bar_5m v0; frozen input-set identity |

Миграция: новый процесс с флагами lean+bars; не переключать mid-run на том же дневном корне без dual-read. Compaction/backup тиков и баров — раздельные корни/префиксы. Для durable bars source и compacted roots разделены; legacy backlog не мигрируется массово.
