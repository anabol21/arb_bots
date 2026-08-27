# Контракт хранения спредов (трек сбора)

Единый список колонок тела `parquet` и раскладки партиций.

Источники правды в коде:

- тики v1: [`app/schema/spread_event.py`](../app/schema/spread_event.py)
- тики `lean` + бары `bar_5m`: [`app/schema/lean_event.py`](../app/schema/lean_event.py)
- раскладка `hive`: [`app/schema/parquet_layout.py`](../app/schema/parquet_layout.py)
- интервалы обрыва WS: [`app/schema/ws_gap.py`](../app/schema/ws_gap.py) (JSONL, не тело lean)

Взгляд модели (гир 1–2): [`docs/data-format-model.md`](data-format-model.md).  
Разрыв между приёмом котировок и моделью: [`docs/data-format-ingest-gap.md`](data-format-ingest-gap.md).  
Операционное описание `lean`: [`docs/local-lean-collector.md`](local-lean-collector.md).

Вход среды исполнения: `app/screaner_b_o.py` → нормализация/запись: `app/storage/writer.py`.

Флаги режима (по умолчанию **выкл.** → v1; производственное накопление тиков → `lean`):

| Флаг | Эффект |
|------|--------|
| `SPREAD_LEAN_SCHEMA=1` | тело тика = `lean` |
| `SPREAD_COLLECT_BARS=1` | слой OKX `candle5m` → `bar_5m`; **production unit с 2026-08-20 выкл.** |

---

## Путь на диске — тики

```text
<SPREADS_ROOT>/base_coin=<COIN>/event_date=<YYYY-MM-DD>/<batch_or_part>.parquet
```

- `base_coin`, `event_date` — `hive`-партиции в пути.
- В теле `lean` / `bar_5m` колонка `base_coin` дублируется для удобства чтения; `event_date` в теле **нет** (`writer` отбрасывает).

Корни по умолчанию: `SPREAD_PARQUET_ROOT=/data/live`, `SPREAD_BARS_ROOT=/data/bars`, `SPREAD_GAPS_ROOT=/data/gaps`.

**Конечная копия (решение 2026-08-16, основа склейки):**

- **Тики:** удалённо `backup1tb:spread-compacted` (`live` → `compact` → выгрузка). Каталог `/data/live` на сервере — первая запись, не конечная копия.
- **Бары:** `/data/bars` на сервере (~1.5 ГиБ на срезе 2026-08-16). Удалённая выгрузка баров и таймеры уплотнения баров **не** обязательны для этого перехода. Одна копия: потеря сервера = потеря истории баров. Тики от этого не зависят.

Сводка готовности сбора: [`d-track-ready-for-b.md`](d-track-ready-for-b.md).

---

## Колонки тела — полный контракт v1 (`canary` / устаревший)

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
- `event_local_ts_ms` = `recv` триггера (`okx` или `bybit` по `trigger`)
- `max_freshness_ms` / `max_latency_ms` = максимум по двум биржам

Публичная книга `L1`: лучший `bid`/`ask` (цена + размер). Суффикс объёма — `_size`.

---

## Колонки тела — тики `lean` (целевой производственный формат)

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

- `spread_long` / `spread_short` — из `L1`  
  - long = `(bybit_bid − okx_ask) / bybit_bid × 100`  
  - short = `(okx_bid − bybit_ask) / okx_bid × 100`
- `*_latency_ms`, `*_freshness_ms`, `max_*`, `event_dt`

Единицы лота / тика / минимального размера — **не** в `parquet`; соединение из [`bybit_okx_universe.csv`](../bybit_okx_universe.csv) по `base_coin`.

Не смешивать `lean` и v1 в одной дневной партиции без ридера с двойным чтением.

---

## Колонки тела — `bar_5m` v0

Отдельный набор данных, не смешивать с пакетами тиков:

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

| Биржа | Точка входа | Канал | Когда писать | Поле | Единица |
|-------|-------------|-------|--------------|------|---------|
| OKX (канон) | `business` `wss://ws.okx.com:8443/ws/v5/business` | `candle5m` | `confirm == "1"` | `volCcy` | базовая монета (`SWAP`) |
| Bybit (опц.) | `linear public` | `kline.5.{symbol}` | `confirm == true` | `volume` | базовая монета |

Не включать: `OHLC`, амплитуду, спреды, `n_updates`, колонки единиц.

---

## Устойчивые `bar_5m` v2 — уплотнённая раскладка

**Источник (до 2026-08-20):** сборщик мог писать исходные пакеты в
`/data/bars/bar_5m` при `SPREAD_COLLECT_BARS=1`. **С 2026-08-20 production
collector бары не собирает и не пишет** — нет `candle5m` WS и нет bar publisher.
Гир 1.5+ берёт 5m с REST-истории (см. [`research/download_okx_bar5m_hist.py`](../research/download_okx_bar5m_hist.py)), не из живого WS. **Уплотнитель** существующих
локальных баров — отдельный процесс. **Потребители:** резервная копия уже
накопленного слоя и историческая модель. Тело `parquet` остаётся
`LEAN_BAR_5M_BODY_COLS`.

```text
# изменяемый локальный источник; только сборщик
/data/bars/bar_5m/base_coin=<COIN>/event_date=<UTC-date>/batch_*.parquet

# уплотнённая публикация v2; одна монета × закрытое часовое окно UTC
/data/bars_compacted_v2/bar_5m/base_coin=<COIN>/event_date=<UTC-date>/
  bar_5m_<YYYYMMDDTHHMMSSZ>_<YYYYMMDDTHHMMSSZ>_inputset=<16-hex>.parquet
```

- Ключи `hive` остаются `base_coin`, `event_date`; они в пути, `event_date` не
  добавляется в тело. `base_coin` остаётся колонкой тела.
- Окно имеет `[window_start, window_end)` в UTC, по умолчанию 3600 с. Оно
  допускается только после `window_end + grace`, а исходный пакет должен быть
  неизменным до этой же границы; текущий/открытый час не уплотняется. Первый
  манифест замораживает точный список путей, размеров, строк и SHA-256 исходников. Поздний
  пакет для уже замороженного часа не добавляется и не создаёт второй
  выход, видимый модели: он остаётся в корне источника и получает запись
  карантина / оповещение. Пакет, пересекающий границу окна, пропускается.
- Публикация: `.inprogress` → `fsync` / повторное чтение (строки + схема) → атомарное переименование
  в конечный файл. Имя конечного файла включает дайджест замороженного набора входов, поэтому
  удалённый идентификатор не сталкивается между разными наборами входов. Сопровождающий манифест
  содержит версию раскладки, пути/размеры/SHA-256 источников, число строк, SHA-256/байты
  выхода и статус жизненного цикла. Существующий конечный файл принимается только при
  точном совпадении контрольной суммы; другая сумма уходит в карантин, без перезаписи.
- После локальной публикации источник переносится только в локальный архив.
  Его срок хранения разрешён лишь когда отдельный манифест выгрузки подтвердил
  конечный объект на `backup1tb:spread-bars-compacted-v2` (`sent` после
  `temporary` → `final`, удалённый размер и проверка SHA). Локальное уплотнение само
  по себе **не** является границей устойчивого удалённого хранения.
- Временные и неполные `.inprogress`, `.tmp` и незавершённые манифесты не являются
  входом модели и не передаются как конечные файлы.

### Совместимость и миграция

Это добавочная версия раскладки **2**, обратимо совместимая на уровне `parquet`:
существующий читатель модели, который рекурсивно читает `hive` `*.parquet`, получает те
же обязательные колонки баров. Читатель должен явно выбрать один корень:
устаревший источник `/data/bars/bar_5m` или устойчивое уплотнение v2
`/data/bars_compacted_v2/bar_5m`; нельзя читать оба одновременно, иначе будут
дубликаты. Устаревшее удалённое `backup1tb:spread-bars` остаётся историческим,
выключено и не очищается и не удаляется без отдельного разрешения. Ранее созданные v1
`/data/bars_compacted` и `backup1tb:spread-bars-compacted` остаются
только для чтения: v2 не выполняет их миграцию, удаление, перезапись или массовое перемещение.
Новые уплотнённые объекты идут только в
`/data/bars_compacted_v2/bar_5m` и `backup1tb:spread-bars-compacted-v2`.

---

## Интервалы обрыва WS (не тело тика)

Кратковременный обрыв книги (обычно 1–4 с) **не** выбрасывает тики до/после в том же слоте уплотнения 5 мин. Сборщик по-прежнему не пишет тик, пока нет обеих свежих ног. Чтобы учёт и симулятор видели дыру длиной обрыва, а не «весь слот потерян», интервал пишется отдельно от lean:

```text
<SPREAD_GAPS_ROOT>/event_date=<YYYY-MM-DD>/gaps.jsonl
```

По умолчанию `SPREAD_GAPS_ROOT=/data/gaps`. Это не дерево D-тиков (`/data/live`) и не `/data/bbot`.

Одна закрытая строка JSONL на пару `ws_disconnect` → парный `ws_subscribe_ok` того же `(exchange, channel, base_coin)`:

| Поле | Тип | Смысл |
|------|-----|--------|
| `schema_version` | int | сейчас `1`; для обнаружения старых строк |
| `base_coin` | str | монета |
| `exchange` | str | `okx` / `bybit` |
| `channel` | str | например `books5`, `orderbook.1` |
| `t_down_ms` | int | UTC wall-clock мс первого обрыва до подъёма |
| `t_up_ms` | int | UTC wall-clock мс парного `subscribe_ok`; `≥ t_down_ms` |
| `close_code` | int или null | код закрытия сокета (`1006`, …) |

`event_date` в пути — календарный UTC-день `t_down_ms`, не поле тела. Тики слота 5 мин остаются входом гиров 1.0 / 2; неполный **интервал**, не весь слот.

Совместимость: новый слой. Отсутствие `/data/gaps` не ломает чтение lean. Старые `runtime.log` без JSONL покрываются парным разбором `ws_disconnect` / `ws_subscribe_ok` в [`validation/check_tick_coverage.py`](../validation/check_tick_coverage.py).

---

## Вне контракта

- Приватные каналы (задержка заявки, аккаунт).
- `L2+`, лента сделок, `funding`, `OI`.
- Переименование `_size` → `_quantity`.

---

## Устаревшее

Файлы без 8 колонок книги допустимы как исторический хвост, но **не** как целевой производственный формат.  
Проверка резервной копии v1 `canary`: [`validation/check_backup_validity.py`](../validation/check_backup_validity.py) ожидает полный `EXPECTED_BODY_COLS` (= тело v1 выше).

---

## Версии и совместимость

| Версия | Когда | Статус |
|--------|-------|--------|
| **v1** | `canary` / флаги выкл. | заморожен; полное тело тика |
| **lean** | `SPREAD_LEAN_SCHEMA=1` | целевой производственный формат для накопления |
| **bar_5m** v0 | `SPREAD_COLLECT_BARS=1` | добавочный слой; отсутствие не ломает тики |
| **bar_5m compacted** v2 | отдельный уплотнитель + выгрузка | устойчивая раскладка; тело = `bar_5m` v0; замороженный идентификатор набора входов |
| **ws_gap** v1 | журнал обрыва WS | JSONL `/data/gaps`; не колонка lean; отсутствие не ломает тики |

Миграция: новый процесс с флагами `lean` + бары; не переключать посреди прогона на том же дневном корне без двойного чтения. Уплотнение и выгрузка тиков и баров — раздельные корни/префиксы. Для устойчивых баров корни источника и уплотнения разделены; устаревший хвост очереди не мигрируется массово.
