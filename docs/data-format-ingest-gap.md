# Gap: запрос модели vs ingest (тики + бары)

Дизайн-заметка + статус интеграции.

**Production lean path (Option B):** код в `app/screaner_b_o.py` / `app/storage/writer.py` пишет lean ticks + OKX `candle5m` bars за флагами (`SPREAD_LEAN_SCHEMA`, `SPREAD_COLLECT_BARS`), default **OFF**. Контракт заморожен в [`docs/storage-contract.md`](storage-contract.md). Для накопления — новый процесс с флагами ON (unit в `deploy/systemd/spread-collector.service`). Операционка: [`docs/local-lean-collector.md`](local-lean-collector.md), [`docs/prod-unit-snippets.md`](prod-unit-snippets.md).

Канон тиков v1: [`app/schema/spread_event.py`](../app/schema/spread_event.py), [`docs/storage-contract.md`](storage-contract.md).  
Lean body: [`app/schema/lean_event.py`](../app/schema/lean_event.py).  
Источник записи: `app/screaner_b_o.py` → `write_spread_record` → `app/storage/writer.py` (`normalize_records`, modes `v1`/`lean`/`bar_5m`).

---

## 1. Pipeline block

```text
OKX books5 / Bybit orderbook.1
  → in-memory quotes[base_coin]{bid/ask price+size, ts, recv, latency}
  → calc_and_store_spread (считает spread_* из L1)
  → opportunities_buffer → ParquetPublisher
  → normalize_records (добавляет event_dt, freshness, max_*)
  → hive: SPREADS_ROOT/base_coin=…/event_date=…/*.parquet

Слой bar_5m:          ❌ нет подписки, нет writer, нет пути
Агрегаты volume/OHLC: ❌ и не нужны в hot path (модель офлайн)
```

| Контекст | Где |
|----------|-----|
| Код | локальный репозиторий |
| Исполнение | VPS canary (не трогаем) |
| Тики на диске | полный body v1 + L1 |
| Бары | отсутствуют |

---

## 2. Таблица: поле модели → сегодня → store vs derive

Легенда статуса: **yes** / **partial** / **no**.

### 2.1 Тики — обязательное ядро модели

| Поле (модель) | Сегодня | Источник WS | Store vs derive | Cost / risk |
|---------------|---------|-------------|-----------------|-------------|
| `event_dt` | **yes** (writer) | из `event_local_ts_ms` | **derive** при чтении | дубль времени; ~8B/row; можно не писать в lean |
| `event_local_ts_ms` | **yes** (writer: recv триггера) | OKX/Bybit local recv | **store** (удобный ключ сортировки) или derive из `trigger`+recv | дёшево; лучше оставить |
| `base_coin` | **yes** (body + partition) | CSV universe | **store** в body *или* только partition | body дублирует hive; для pyarrow/pandas удобно оставить |
| `trigger` | **yes** | кто обновил quotes | **store** | нужно для `Trade_Lat`/окон |
| `okx_latency_ms` / `bybit_latency_ms` | **yes** | `local_recv − exchange_ts` в listener | **derive** из `*_local_recv_ts_ms` − `*_ts_ms` | хранение = удобство гейтов; ~16B |
| `max_latency_ms` | **yes** (writer) | max двух | **derive** | не нужен в lean |
| `okx_freshness_ms` / `bybit_freshness_ms` | **yes** (writer) | `calc − local_recv` | **derive** | опц. гейт; не обязателен в lean body |
| `max_freshness_ms` | **yes** (writer) | max двух | **derive** | то же |
| `calc_local_ts_ms` | **yes** | `time.time()*1000` в calc | **store** | примитив для freshness |
| `okx_local_recv_ts_ms` / `okx_ts_ms` | **yes** | OKX books5 `ts` + local | **store** | аудит/гейты |
| `bybit_local_recv_ts_ms` / `bybit_ts_ms` | **yes** | Bybit `ts` + local | **store** | аудит/гейты |
| L1 8× price/size | **yes** (полный контракт) | top of book | **store** | ядро; без этого гир 2+ / volume gate на стакане слабый |
| `spread_long` / `spread_short` | **yes** (v1) | считаются в calc из L1 | **derive** при чтении | модель **не требует**; оставить до конца валидации бэкапа |

Имена в модели (`okx_*_ts_ms`) = алиас к фактическим `okx_ts_ms` / `okx_local_recv_ts_ms` (и Bybit). Предлагаемые правки в model-doc — только уточнение канона имён, не новые поля.

### 2.2 Тики — nice-to-have / не в запросе модели

| Поле / тема | Сегодня | Store vs derive | Заметка |
|-------------|---------|-----------------|---------|
| Bybit `cts` (matching ts) | в памяти quotes, **не** в parquet | не хранить, пока модель не попросит | аудит; раздувает schema |
| OKX books5 уровни 2–5 | парсятся только L1 | **не store** | правильно lean |
| Единицы `_size` (контракты vs base) | **partial** семантика | store как есть + **документировать** | риск для volume gate между биржами; не новая колонка |
| Амплитуда / mid / OHLC | нет | **derive** офлайн из L1 | не писать |
| Перцентили volume | нет | **derive** в модели из баров | не писать |

### 2.3 Слой баров `5m` (запрос модели, гир 1.5)

| Поле | Сегодня | Источник (кандидат) | Store vs derive | Cost / risk |
|------|---------|----------------------|-----------------|-------------|
| `bar_start_ts_ms` | **no** | candle/kline channel | **store** | новый слой; низкая частота vs тики |
| `bar_end_ts_ms` | **no** | start+5m | **derive** или store | 5 мин фиксированы → можно derive |
| `base_coin` | **no** (в барах) | universe | store / partition | как у тиков |
| `ref_exchange` | **no** | константа `okx` (канон модели) | **store** или default в ридере | 1 строка/бар |
| `volume` | **no** | канал баров опорной биржи | **store** | **единственный обязательный новый primitive** |
| OHLC / amplitude в баре | не просят | — | **не store** | из тиков |
| `n_updates` | не просят | — | **derive** из тиков при нужде | не раздувать бар |

---

## 3. Что реально missing на уровне ingest

### Для гиров 1–2 (тики; мультимонета с гира 2)

**Почти ничего.** Полный L1 + метки времени/задержки уже пишутся. Модель может:

- считать `spread_*` из L1;
- считать mid/амплитуду офлайн;
- гейтить по latency/freshness (колонки есть или выводятся из stamps).

Остаточные **не-дыры схемы, а риски смысла**:

1. **Единицы L1 size** OKX vs Bybit не зафиксированы в контракте → volume gate на тике сигнала может сравнивать яблоки с апельсинами.
2. **Legacy-партиции без 8 book-колонок** — для гира 2+ не смешивать с полным L1 (уже сказано в model/strategy docs).
3. Избыток производных в v1 (`spread_*`, `max_*`, freshness, `event_dt`) — не missing, а **bloat** относительно цели модели.

### Для гира 1.5 (скринер режима; и дальше)

**Главный gap:** нет слоя **`bar_5m` с `volume`** (нужен уже для скринера 1.5, не только для размера в 2.5).

- Нет WS-подписки на свечи/kline в canary path (local lean — см. §9).
- Нет отдельного root/writer/partition layout в проде.
- Семантика `volume` для OKX зафиксирована в §9.1 (`volCcy`); canary пока без bars.

Без баров перцентили/`volume/median` для **флага режима** строить честно не из чего (аппроксимация суммой L1 size по тикам — другая величина, не объём свечи).

---

## 4. Lean schema proposal

### 4.1 Тики — keep / drop / add

| Действие | Колонки | Когда |
|----------|---------|--------|
| **KEEP (примитивы)** | `trigger`, `calc_local_ts_ms`, `okx_local_recv_ts_ms`, `okx_ts_ms`, `bybit_local_recv_ts_ms`, `bybit_ts_ms`, 8× L1 | всегда |
| **KEEP (удобство, дёшево)** | `event_local_ts_ms`, `base_coin` в body | рекомендуется |
| **KEEP временно (v1 freeze)** | `spread_long`, `spread_short`, latency, freshness, `max_*`, `event_dt` | до закрытия canary/backup validity |
| **DROP в целевом lean (v2 candidate)** | `spread_*`, `max_*`, freshness; опц. `event_dt`, опц. `*_latency_ms` | только после dual-read в модели + новая версия контракта |
| **ADD в тики** | ничего | не нужно для model request |

Целевой lean body (~15–17 колонок vs 25):

```text
event_local_ts_ms, base_coin, trigger,
calc_local_ts_ms,
okx_local_recv_ts_ms, okx_ts_ms,
bybit_local_recv_ts_ms, bybit_ts_ms,
okx_bid_price, okx_bid_size, okx_ask_price, okx_ask_size,
bybit_bid_price, bybit_bid_size, bybit_ask_price, bybit_ask_size
```

Всё остальное из model-ядра — формулы при чтении (`spread_*`, latency, freshness, `event_dt`, `max_*`).

### 4.2 Бары — новый контракт `bars_5m` v0 (add-only)

Отдельный root, **не** смешивать с тиковыми batch:

```text
<BARS_ROOT>/bar_5m/base_coin=<COIN>/event_date=<YYYY-MM-DD>/….parquet
```

Минимальный body:

| Колонка | Обязательность |
|---------|----------------|
| `bar_start_ts_ms` | store |
| `base_coin` | store или только partition |
| `ref_exchange` | store (`okx`) или default в ридере |
| `volume` | store |
| `bar_end_ts_ms` | optional (derive = start + 300_000) |
| `volume_unit` | **рекомендуется** одна строковая константа на файл/колонка (`base_coin` / `contracts` / `quote`) — без неё модель гадает |

Не включать: OHLC, amplitude, spreads, `n_updates`, перцентили.

**Канал (решение отдельно, не unlock сейчас):** публичный candle/kline опорной биржи (`okx`), interval `5m`, только закрытый бар → одна строка. Не агрегировать из orderbook в hot path.

### 4.3 Что сознательно не добавляем

- L2+, trades tape, funding, OI
- private latency / `Trade_Lat`
- дубль mid/amplitude в тике или баре
- второй биржевой volume «на всякий» без явного запроса модели

---

## 5. Миграция / совместимость (canary не ломать)

Инварианты:

1. **Тики v1 body заморожен** на время валидации бэкапа/compaction (`EXPECTED_BODY_COLS` = полный список).
2. Урезание колонок = **новая версия контракта**, не silent drop в mid-canary.
3. Слой баров = **additive** path + optional service/writer; отсутствие баров не ломает тиковый pipeline.

План совместимости:

| Фаза | Действие | Риск для prod path |
|------|----------|--------------------|
| A | Документ gap + решение по семантике `volume` / единицам L1 size | нулевой |
| B | Model reader: dual-path — если нет `spread_*`, считать из L1 | нулевой на writer |
| C | **Сделано в коде:** bars writer/root + `SPREAD_COLLECT_BARS` (default off); lean ticks + `SPREAD_LEAN_SCHEMA` (default off) | нулевой, пока флаги off |
| D | **Unlocked:** включить флаги на новом запуске после soak; unit несёт lean+bars; не смешивать с v1 day partition | операционный (диск, cutover) |

Чтение истории:

- Старые файлы со `spread_*` — валидны для гира 1.0.
- Новые lean без `spread_*` — модель обязана уметь derive.
- Партиции без L1 — только legacy; не для гира 2+.

Compaction/backup: бары — отдельный префикс remote; не смешивать checksum/row expectations тиков с барами.

Размер: бар `5m` на монету ≈ 288 строк/сутки ≪ тиковый поток; влияние на backup пренебрежимо vs выигрыш от будущего drop `spread_*`/max/freshness (~30% колонок body).

---

## 6. Риски и failure modes

| Риск | Почему важно |
|------|----------------|
| Считать gap «нужно переписать ingest тиков» | Тики уже покрывают гир 1–2; переписывать WS ради lean — лишний риск canary |
| Писать volume в тиковый parquet | Смешает частоты, раздует batch, усложнит compaction |
| Aggregated «volume» из L1 size | Не объём свечи; исказит скринер гира 1.5 и дальше |
| Не зафиксировать unit `volume` / `_size` | Тихие ошибки volume gate и position sizing |
| Drop `spread_*` до dual-read в модели | Сломает текущий `model.ipynb` / гир 1 на новых файлах |
| Новый bars writer на том же hot path без spool | Mount latency на редких барах менее критична, но всё равно нужна явная durability semantics |

---

## 7. Рекомендуемый следующий шаг (эксперимент, не реализация)

**Не unlock ingest.** Согласовать с пользователем три решения на 1 страницу (можно комментарием в этом файле):

1. **Семантика `volume` для `ref_exchange=okx`:** какая единица и какой публичный канал (кандидат: OKX candle `5m`, поле vol / volCcy — выбрать одно и зафиксировать в storage-contract как bars v0).
2. **Единицы L1 `_size`:** одна строка в `storage-contract.md` (OKX books size = ?; Bybit orderbook.1 size = ?) — без новых колонок.
3. **Приоритет:**  
   - тики полного L1 — база гиров 1.0 и 2;  
   - **бары `5m` — приоритет под гир 1.5** (скринер); отдельный bars pipeline (schema + path + mount validation), тики не ломать;  
   - lean drop колонок — отдельно, не блокер скринера.

Опционально после согласования (1): маленький design-only PR — `BAR_5M_BODY_COLS` stub в `app/schema/` + абзац в `storage-contract.md` (без WS), чтобы контракт существовал до unlock.

Success criteria этого шага: письменное «да» на unit/channel volume и решение keep-v1 vs plan-v2 lean; canary и тиковый writer без изменений.

---

## 8. Suggested edits to `data-format-model.md` (не внесены)

- Явно перечислить канон имён stamps: `okx_ts_ms`, `okx_local_recv_ts_ms`, … (вместо `okx_*_ts_ms`).
- Добавить ссылку на этот gap-doc.
- В блоке баров: placeholder `volume_unit` как рекомендованное поле контракта сборщика (модель может игнорировать, если unit глобален).

---

## 9. Решения (2026-08-04): каналы баров + единицы + lean local track

Зафиксировано для **локального** параллельного трека ([`docs/local-lean-collector.md`](local-lean-collector.md)). Canary / `app/screaner_b_o.py` / `/data/live` **не** менялись.

### 9.1 Канал `volume` для `bar_5m`

| Биржа | WS | Канал / topic | Когда писать | Поле `volume` | Единица |
|-------|----|---------------|--------------|---------------|---------|
| **OKX (канон `ref_exchange=okx`)** | `wss://ws.okx.com:8443/ws/v5/business` | `candle5m` + `instId` | `confirm == "1"` | **`volCcy`** | base coin (SWAP) |
| Bybit (опционально) | `wss://stream.bybit.com/v5/public/linear` | `kline.5.{symbol}` | `confirm == true` | `volume` | base coin (linear USDT) |

- OKX свечи — на **business** WS, не на public.
- Не использовать OKX `vol` (контракты) как канон для модели.
- REST fallback: OKX `GET /api/v5/market/candles?bar=5m`; Bybit `GET /v5/market/kline?interval=5`.

### 9.2 Единицы L1 / контрактов — **не** в parquet

Метаданные размеров уже в [`bybit_okx_universe.csv`](../bybit_okx_universe.csv) (`okx_lot_size`, `okx_min_size`, `bybit_qty_step`, …).  
**Join по `base_coin` при анализе.** В lean/canary parquet **не** писать unit-колонки (`volume_unit`, lot/tick sizes).

### 9.3 Lean local collector (фаза эксперимента, не unlock canary)

- Скрипт: `app/screaner_local_lean.py`
- Схема: `app/schema/lean_event.py` (отдельно от `spread_event.py`)
- Корни по умолчанию: `output/lean_ticks`, `output/lean_bars` (env `SPREAD_LEAN_*`)
- Простой parquet write (стиль раннего screaner); без spool/publisher canary

---

## Версия заметки

- **2026-08-04** — первичный gap vs model request; тики ≈ полны; missing = bars `volume` + семантика единиц.
- **2026-08-04 (update)** — выбраны OKX `candle5m`/`volCcy` (+ optional Bybit `kline.5`); единицы только из CSV; добавлен local lean track.
- **2026-08-04 (gears)** — бары привязаны к **гиру 1.5** (скринер режима); поиск параметров сдвинут на гир 3; см. [`docs/strategy-gears.md`](strategy-gears.md).
