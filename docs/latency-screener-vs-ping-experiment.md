# Эксперимент: latency скринера vs ping_dual

**Трек:** collection / storage reliability (наблюдаемость задержки, не стратегия).  
**Статус:** controlled ping на **matched XRP** запущен; коллектор не трогали.  
**Цель:** проверить, добавляет ли скринер *паразитическую* задержку поверх сетевой/биржевой, или пики — внешние.

Дата дизайна: 2026-08-05.  
**Текущий ping (matched):** start `2026-08-05T13:00:27Z` → end ≈`2026-08-05T19:00:27Z` (22:00 MSK).  
Лог: `/var/log/spread/ping_dual_6h_xrp.log` · pid: `/var/log/spread/ping_dual_6h_xrp.pid`.  
Старый mismatched лог (OKX=BTC, Bybit=XRP): `/var/log/spread/ping_dual_6h.log.bak_btc_okx_xrp_bybit_20260805T130027Z`.

---

## 1. Pipeline block

| Слой | Что сравниваем |
|------|----------------|
| Ingest | WS books (OKX `books5`, Bybit `orderbook.1`) |
| Метрика | возраст котировки = `local_recv − exchange_ts` (**не ICMP**) |
| Screener | `app/screaner_b_o.py` → lean ticks → `/data/live` |
| Ping | `validation/ping_okx_bybit_2h.py` → `/var/log/spread/ping_dual_6h_xrp.log` (matched XRP) |
| Анализ | офлайн notebook / скрипт по пересечению wall-clock окна |

Заморожено: логика подписок, парсинг стакана, формула спреда — **не трогаем**. Меняем только способ *измерения и сравнения*.

---

## 2. Существующие модули и определения

### 2.1 Формула latency (канон)

| Источник | Формула | Поля |
|----------|---------|------|
| Screener listener | `delivery_latency_ms = local_recv_ts_ms − ts_exchange` | OKX: `payload.ts`; Bybit: корневой `data.ts` (**не `cts`**) |
| v1 parquet | пишется `okx_latency_ms` / `bybit_latency_ms` | то же значение |
| lean parquet | latency **не пишется** | stamps остаются — derive при чтении |
| ping OKX | `latency_ms = local_ms − data[0].ts` | `books5` / `XRP-USDT-SWAP` (`--okx-inst` / `PING_OKX_INST`) |
| ping Bybit | `latency_ms = age_ts_ms = local_ms − ts`; также `age_cts_ms` | `orderbook.1.XRPUSDT` (`--bybit-symbol` / `PING_BYBIT_SYMBOL`) |

Derive для lean (идентично runtime):

```text
okx_latency_ms   = okx_local_recv_ts_ms   - okx_ts_ms
bybit_latency_ms = bybit_local_recv_ts_ms - bybit_ts_ms
```

Дополнительно (не путать с latency):

```text
okx_freshness_ms   = calc_local_ts_ms - okx_local_recv_ts_ms
bybit_freshness_ms = calc_local_ts_ms - bybit_local_recv_ts_ms
```

`freshness` ≈ стоимость calc после штампа recv. `latency` уже включает задержку до входа в callback (event-loop / scheduling), т.к. `local_recv` берётся **в начале** обработки сообщения (до `json.loads`).

Код: `app/screaner_b_o.py` (listeners + `calc_and_store_spread`), схемы: `app/schema/lean_event.py`, `app/schema/spread_event.py`, ping: `validation/ping_okx_bybit_2h.py`, `ping_okx.py`, `ping_bybit.py`.

### 2.2 Lean: stamps есть, колонок latency нет

`LEAN_TICK_BODY_COLS` явно без `*_latency_ms` / freshness / spread (комментарий: *derive at read*). Хранятся:

- `okx_local_recv_ts_ms`, `okx_ts_ms`
- `bybit_local_recv_ts_ms`, `bybit_ts_ms`
- `event_local_ts_ms`, `calc_local_ts_ms`, `trigger`, L1

**Вывод:** текущий lean 6h-ран пригоден для сравнения latency без перезапуска коллектора.

### 2.3 Где читать файлы (VPS)

```text
# Screener lean ticks (пример)
/data/live/base_coin=BTC/event_date=YYYY-MM-DD/batch_*.parquet
/data/live/base_coin=0G/event_date=YYYY-MM-DD/batch_*.parquet
/data/live/base_coin=XRP/event_date=YYYY-MM-DD/batch_*.parquet

# Ping dual log (matched XRP)
/var/log/spread/ping_dual_6h_xrp.log
# Prior mismatched run (OKX=BTC, Bybit=XRP)
/var/log/spread/ping_dual_6h.log.bak_btc_okx_xrp_bybit_20260805T130027Z
```

`event_date` = UTC-дата из `event_local_ts_ms`. Для окна, пересекающего полночь UTC, читать **две** партиции. Не путать с `/data/live/archived/` (после compaction) — для «живого» окна сначала active batches.

Скелет анализа: `research/latency_screener_vs_ping.ipynb`.

---

## 3. Apples-to-apples: что сравнимо, что нет

### 3.1 Символы — главный confounder (снят для текущего ping)

| Процесс | OKX | Bybit | Масштаб |
|---------|-----|-------|---------|
| Screener | весь universe (~345 пар из CSV; наблюдения ~337) | те же base_coin | N×2 WS на одном asyncio loop |
| ping_dual **сейчас** | `XRP-USDT-SWAP` | `XRPUSDT` | 2 независимых thread + sync WS |
| ping_dual **старый bak** | `BTC-USDT-SWAP` | `XRPUSDT` | mismatched (не для apples-to-apples) |

Universe (`bybit_okx_universe.csv`): `base_coin=XRP` → OKX `XRP-USDT-SWAP`, Bybit `XRPUSDT`.

**Сравнение tonight (matched XRP):**

```text
S_okx    = lean base_coin=XRP, trigger=="okx"   → derive okx_latency_ms
S_bybit  = lean base_coin=XRP, trigger=="bybit" → derive bybit_latency_ms
P_okx    = ping exchange==okx   (XRP-USDT-SWAP)
P_bybit  = ping exchange==bybit (XRPUSDT), age_ts_ms
```

Не агрегировать все 337 монет против XRP ping. Старый bak-лог — только как historical mismatch reference.

CLI/env: `--okx-inst` / `PING_OKX_INST`, `--bybit-symbol` / `PING_BYBIT_SYMBOL`.

### 3.2 Семантика события скринера

Событие пишется при обновлении **любой** ноги (`trigger=okx|bybit`). В строку попадают **оба** `*_latency_ms` (или derive stamps):

- для **trigger-ноги** latency = delivery latency **только что** пришедшего сообщения → сопоставимо с ping sample;
- для **не-trigger ноги** latency = delivery latency **последнего** сообщения той биржи (снимок в `quotes[...]['delivery_latency_ms']`), а не «возраст книги сейчас».

Ошибка новичков: смотреть `okx_latency` и `bybit_latency` на одной строке как «одновременную доставку обеих бирж».  
**Правило сравнения с ping:**

```text
trigger_latency_ms =
  okx_latency_ms   if trigger == "okx"
  bybit_latency_ms if trigger == "bybit"
```

Отдельно считать dual-spike метрику (ниже) — она про *оба* снимка latency в одной строке, но интерпретируется как «обе последние доставки были плохими», часто из‑за общего stall loop / скачка локальных часов.

### 3.3 Почему dual-spike на скринере ≠ dual-spike на ping

- Screener: один event loop, сотни сокетов, JSON, calc, буфер, периодический flush/write.
- Ping: два потока, по одному символу, почти no-op после измерения.
- Одновременные пики **обеих** ног в скринере при «тихом» ping → сильный сигнал **локального** блокирования / clock, а не биржи.
- Пики только Bybit в ping (~500 ms, часто на старте) при dual 1000+ ms в скринере → скринерные dual-spikes **не объясняются** одним Bybit path.

### 3.4 Clock / определение (cts vs ts)

- Screener Bybit latency всегда на `ts`.
- Ping логирует `age_ts_ms` и `age_cts_ms`. Для apples-to-apples с скринером брать **`age_ts_ms` / `latency_ms`**, не cts.
- Скачок NTP/`time.time()` вверх раздувает latency на всех ногах сразу; скачок вниз даёт отрицательные/аномально малые значения — отдельно отфильтровать / пометить.

---

## 4. Дизайн эксперимента

### 4.A Matched-XRP прогон (текущее 6h окно) — коллектор не стопать

**Окно:** пересечение wall-clock скринера и ping (из meta `start` / `finished` в `ping_dual_6h_xrp.log` + `min/max(event_local_ts_ms)` по lean `base_coin=XRP`).

**Выборки:**

| Серия | Фильтр | Метрика |
|-------|--------|---------|
| S_okx | lean `base_coin=XRP`, `trigger=="okx"` | derive `okx_latency_ms` |
| S_bybit | lean `base_coin=XRP`, `trigger=="bybit"` | derive `bybit_latency_ms` |
| P_okx | ping `exchange==okx` | `latency_ms` |
| P_bybit | ping `exchange==bybit` | `age_ts_ms` (== `latency_ms`) |

**Выравнивание:**

1. Обрезать по `[t0, t1]` пересечения.
2. Первичный слой: **минутные бакеты** UTC — в каждом бакете p50/p95/p99/max/count по каждой серии.
3. Вторичный: nearest-neighbour в ±250 ms только внутри одной биржи (S_okx↔P_okx, S_bybit↔P_bybit) для scatter; не матчить OKX↔Bybit.
4. Dual-spike скринера (отдельный анализ): строки XRP (или любая монета — для «loop stall»), где `okx_latency` и `bybit_latency` оба > порога (например 500 ms и 1000 ms) в одной записи; счётчик таких минут; сравнить с ping в той же минуте (были ли пики ping?).

**Таблицы:**

- Quantiles floor / p50 / p95 / p99 / max / n — по 4 сериям.
- Δ = screener_pXX − ping_pXX по минутам (медиана Δ, доля минут с Δ>50 ms, >200 ms).
- Co-spike rate: доля минут, где обе ноги скринера (на выбранных монетах) >T, и доля из них, где ping тих.

**Графики:**

1. Time series минутный p95: S vs P по OKX; отдельно Bybit.
2. Scatter минутный p95 screener vs ping (y=x reference).
3. Histogram / ECDF trigger-latency S vs P.
4. Heatstrip dual-spike минут (да/нет) + overlay ping max.

**Контроль freshness:** минутный p95 `calc − trigger_recv`. Если freshness стабильно мал (&lt; few ms), а latency пики высокие — паразит **до** calc (loop/scheduling), не «долгий spread math».

### 4.B Опциональный follow-up (BTC matched)

Текущий controlled run уже на XRP/XRP. При необходимости повторить 1–2h на `BTC-USDT-SWAP` + `BTCUSDT` теми же флагами (коллектор не стопать). Зафиксировать hostname, NTP, loadavg, schema flags, log path.

---

## 5. Исходы и фальсифицируемые интерпретации

| Исход | Наблюдение | Интерпретация | Что отвергает |
|-------|------------|---------------|---------------|
| **A** | CDF/квантили S≈P на trigger-leg, тех же символах; dual-spikes редки или есть и в ping | Нет заметного паразита скринера; пики сеть/биржа/часы | «Скринер всегда раздувает latency» |
| **B** | Floor/p50 близки; хвост S тяжелее; dual-spikes обеих ног скринера при тихом ping | Event-loop / I/O / GIL / flush / scheduling stall | Чистый exchange-side only |
| **C** | p50/p95 S стабильно выше P на десятки–сотни ms | Систематическая обработка / очередь сообщений | «Только редкие спайки» |
| **D** | Bybit пики в S и P; OKX спокоен в P, но S показывает dual 1000+ | Bybit path реален; dual-spikes скринера **не** объяснены биржей одной ногой → нужен B/E | «Всё = Bybit» |
| **E** | Расхождение исчезает при смене определения (cts vs ts), отрицательные latency, синхронный сдвиг обеих ног без нагрузки | Clock skew / mismatch определения | Паразитный CPU путь |

Смешанные исходы нормальны (например D+B). Вердикт формулировать **посимвольно и по квантилю**, не одной фразой.

---

## 6. Риски и failure modes анализа

- Сравнение all-coins screener vs BTC/XRP ping → ложный «паразит».
- Использование non-trigger latency как delivery sample.
- Смешение `freshness` и `latency`.
- Чтение archived/compacted без учёта окна; неверный `event_date`.
- Survivor bias: если write path дропает события при перегрузе, хвост latency в parquet **занижен** (нужны runtime metrics / log gaps).
- Разный clock source: screener `time.time()*1000` vs ping `time.time_ns()//1_000_000` — обычно эквивалентно, но отметить.
- Compaction mid-window: предпочитать файлы, покрывающие окно; не деструктивно трогать `/data/live`.

---

## 7. План валидации (VPS / storage)

Только read-only:

```bash
# Окно ping (matched XRP)
head -20 /var/log/spread/ping_dual_6h_xrp.log
tail -20 /var/log/spread/ping_dual_6h_xrp.log
cat /var/log/spread/ping_dual_6h_xrp.pid
ps -p "$(cat /var/log/spread/ping_dual_6h_xrp.pid)" -o pid,etime,cmd

# Наличие lean stamps для XRP (primary) / BTC / 0G
python3 - <<'PY'
import pyarrow.parquet as pq
from pathlib import Path
for coin in ("XRP", "BTC", "0G"):
    paths = sorted(Path("/data/live").glob(f"base_coin={coin}/event_date=*/batch_*.parquet"))
    print(coin, "nfiles", len(paths))
    if paths:
        t = pq.read_table(paths[-1], columns=[
            "event_local_ts_ms","trigger",
            "okx_local_recv_ts_ms","okx_ts_ms",
            "bybit_local_recv_ts_ms","bybit_ts_ms",
        ])
        print(" cols_ok", t.num_rows, t.column_names)
PY
```

Не: stop systemd, truncate log, delete batches, менять mount.

---

## 8. Success criteria

Минимум для tonight (matched XRP):

1. Зафиксированы `[t0,t1]`, пути (`ping_dual_6h_xrp.log`), schema=lean, derive-формула.
2. Таблица квантилей S vs P для **XRP trigger=okx ↔ OKX ping** и **XRP trigger=bybit ↔ Bybit ping**.
3. Счётчик dual-spike минут + состояние ping в этих минутах.
4. Предварительный ярлык A–E; символ-confounder снят (оба ping на XRP).

---

## 9. Рекомендуемый следующий шаг

**После ≈19:00 UTC (конец matched XRP ping):**

1. Прочитать `ping_dual_6h_xrp.log` и lean parquet `base_coin=XRP` за пересечение.
2. Прогнать `research/latency_screener_vs_ping.ipynb` (скелет обновлён под XRP/XRP).
3. Записать квантили + исход A–E.

Не делать: стоп коллектора, смена schema, правки ingest. Старый bak-лог можно оставить как reference.
