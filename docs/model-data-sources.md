# Источники данных для трека модели

Краткая шпаргалка: **откуда брать историю**, какие пути, какой формат parquet.  
Аудитория: трек модели (гиры 1.0–3), `model.ipynb`, офлайн-симуляция. Не runbook сборщика.

Лестница гиров: [`docs/strategy-gears.md`](strategy-gears.md).  
Контракт колонок (инженерия): [`docs/storage-contract.md`](storage-contract.md).  
Запрос модели к полям: [`docs/data-format-model.md`](data-format-model.md).

---

## 1. Назначение

| Вопрос | Ответ |
|--------|--------|
| Для кого | Симулятор / скринер режима / мультимонета — **не** живой бот |
| Что нужно | Тики L1 (+ спреды при чтении), с гира **1.5** — бары `5m` с `volume` |
| Где правда для исследования | **Durable backup** (`backup1tb:`), не рабочий диск VPS |
| VPS `/data/*` | Рабочий/временный слой (live → compact → sent); для Mac-исследований — не основной SoT |

---

## 2. Рекомендуемый source of truth (накопление вперёд)

| Dataset | Durable remote (предпочтительно для модели) | Локально на VPS (рабочий) |
|---------|---------------------------------------------|---------------------------|
| **Тики** | `backup1tb:spread-compacted/spread_*.parquet` | `/data/live` → compact `/data/compacted` |
| **Бары `bar_5m`** | REST-история OKX (`download_okx_bar5m_hist.py`); старый слой `backup1tb:spread-bars/bar_5m/...` | `/data/bars/bar_5m/...` больше не пишется collector'ом |

Сборщик на VPS: **lean ticks**, без `candle5m` (`SPREAD_LEAN_SCHEMA=1`, `SPREAD_COLLECT_BARS=0`) → `/data/live`. Бары 5m для модели — отдельно по REST, не из живого WS.

**Для модели:** тянуть с remote backup (rclone / scp с хоста бэкапа), а не копировать live-hive с VPS как «архив». Live на VPS короткоживущий (archive/sent retention ~12h).

---

## 3. Таблица путей

| Роль | VPS (локальный диск) | rclone remote | Как на Mac |
|------|----------------------|---------------|------------|
| Тики live (hive) | `/data/live/base_coin=<COIN>/event_date=<YYYY-MM-DD>/batch_*.parquet` | — (не бэкапится как hive) | Не для накопления; только оперативный срез |
| Тики compacted | `/data/compacted/spread_*.parquet` | `backup1tb:spread-compacted/spread_*.parquet` | `rclone copy backup1tb:spread-compacted ./data/ticks/` или scp с SFTP-хоста бэкапа |
| Бары live (hive) | `/data/bars/bar_5m/base_coin=…/event_date=…/batch_*.parquet` | `backup1tb:spread-bars/bar_5m/...` (тот же hive) | `rclone copy backup1tb:spread-bars ./data/bars/` |
| Universe (единицы) | репо / staging | — | `bybit_okx_universe.csv` в корне репо |
| Runtime log | `/var/log/spread/runtime.log` | — | не датасет модели |

Имена compacted-тиков: `spread_<startUTC>_<endUTC>.parquet` — **одно окно ~5 мин, все монеты в одном файле** (не hive).

Пример pull (с машины, где настроен rclone remote `backup1tb`):

```bash
# Тики (flat)
rclone copy backup1tb:spread-compacted ./research_data/ticks/ --include "spread_*.parquet"

# Бары (hive)
rclone copy backup1tb:spread-bars ./research_data/bars/
```

---

## 4. Формат тиков (lean) — текущее накопление

**Контракт:** [`app/schema/lean_event.py`](../app/schema/lean_event.py) → `LEAN_TICK_BODY_COLS` (**16 колонок**).

### Hive на live (VPS)

```text
/data/live/base_coin=<COIN>/event_date=<YYYY-MM-DD>/<batch>.parquet
```

`event_date` — только партиция пути; в body **нет**. `base_coin` дублируется в body.

### Compacted / backup

```text
spread_YYYYMMDDTHHMMSSZ_YYYYMMDDTHHMMSSZ.parquet
```

Строки всех монет окна склеены в один файл. Фильтр по `base_coin` при чтении.

### Колонки lean (int64 ms для stamps; цены/сайзы — numeric)

| # | Колонка |
|---|---------|
| 1 | `event_local_ts_ms` |
| 2 | `base_coin` |
| 3 | `trigger` |
| 4 | `calc_local_ts_ms` |
| 5 | `okx_local_recv_ts_ms` |
| 6 | `okx_ts_ms` |
| 7 | `bybit_local_recv_ts_ms` |
| 8 | `bybit_ts_ms` |
| 9–12 | `okx_bid_price`, `okx_bid_size`, `okx_ask_price`, `okx_ask_size` |
| 13–16 | `bybit_bid_price`, `bybit_bid_size`, `bybit_ask_price`, `bybit_ask_size` |

**Не пишутся** (считать при чтении):

- `spread_long` / `spread_short`
- `*_latency_ms`, `*_freshness_ms`, `max_*`, `event_dt`

### Формулы спредов

```text
spread_long  = (bybit_bid_price − okx_ask_price) / bybit_bid_price × 100
spread_short = (okx_bid_price − bybit_ask_price) / okx_bid_price × 100
```

Latency (как в runtime): `okx_latency_ms = okx_local_recv_ts_ms − okx_ts_ms` (аналогично Bybit).

---

## 5. Формат баров `bar_5m`

**Контракт:** `LEAN_BAR_5M_BODY_COLS` в [`app/schema/lean_event.py`](../app/schema/lean_event.py).

```text
<BARS_ROOT>/bar_5m/base_coin=<COIN>/event_date=<YYYY-MM-DD>/….parquet
```

На VPS: `BARS_ROOT=/data/bars`. На backup: тот же относительный hive под `backup1tb:spread-bars/`.

| Колонка | Смысл |
|---------|--------|
| `bar_start_ts_ms` | начало окна (включительно), int64 ms |
| `bar_end_ts_ms` | `start + 300_000` (исключительно) |
| `base_coin` | монета |
| `ref_exchange` | канон: `okx` |
| `volume` | объём закрытой свечи OKX `candle5m` поле **`volCcy`** (base coin, SWAP) |

Не включать в ожидания модели: OHLC, amplitude, spreads, `n_updates`, unit-колонки в parquet.

Бары **не** компактятся в flat `spread_*`; бэкап — hive as-is.

---

## 6. Единицы через universe CSV

Файл: [`bybit_okx_universe.csv`](../bybit_okx_universe.csv).

Фильтр crypto vs equity/ETF/metal perps: [`research/is_crypto.py`](../research/is_crypto.py) (`is_crypto(base_coin)`).

**Join по `base_coin` при анализе.** В parquet lot/tick/min-size **не** пишутся.

Полезные колонки CSV: `okx_symbol`, `bybit_symbol`, `okx_lot_size`, `okx_min_size`, `bybit_qty_step`, `bybit_min_order_qty`, tick sizes, …

Риск: `_size` на L1 OKX vs Bybit — разные семантики контрактов; для volume gate / sizing сверять с CSV, не угадывать из parquet.

---

## 7. Legacy v1 canary на backup — не путать с lean

| Эра | Где | Schema body | Признак |
|-----|-----|-------------|---------|
| **Canary v1** (≈ Aug 3–4 2026 и раньше) | `backup1tb:spread-compacted/` | полный v1: есть `spread_long`/`spread_short`, latency, freshness, `event_dt`, … (~25 кол.) | колонки `spread_*` в файле |
| **Lean accumulation** (с ~2026-08-05) | тот же remote prefix + новые окна | 16 lean-колонок, **без** `spread_*` | нет `spread_*`; stamps + L1 |

Оба типа лежат в **одном** flat-префиксе `spread-compacted/`. Compactor склеивает окно как есть — **схема может отличаться по эре файла**. Перед concat/filter:

1. Проверить schema / наличие колонок (`spread_long` in schema → v1).
2. Dual-read: если нет `spread_*` — derive из L1; если есть — можно использовать или пересчитать для проверки.
3. Не смешивать v1 и lean в одном pandas concat без выравнивания колонок.

Канон v1: [`app/schema/spread_event.py`](../app/schema/spread_event.py) → `SPREAD_EVENT_BODY_COLS`.  
Гир 1.0 на старых файлах со спредами остаётся валидным; новое накопление — lean.

---

## 8. Минимальные примеры загрузки

### Lean tick + derive spread (один compacted файл)

```python
import pyarrow.parquet as pq
import pandas as pd

path = "research_data/ticks/spread_20260805T120000Z_20260805T120500Z.parquet"
df = pq.read_table(path).to_pandas()
df = df[df["base_coin"] == "XRP"]  # compacted = все монеты

df["spread_long"] = (
    (df["bybit_bid_price"] - df["okx_ask_price"]) / df["bybit_bid_price"] * 100
)
df["spread_short"] = (
    (df["okx_bid_price"] - df["bybit_ask_price"]) / df["okx_bid_price"] * 100
)
df["okx_latency_ms"] = df["okx_local_recv_ts_ms"] - df["okx_ts_ms"]
df["bybit_latency_ms"] = df["bybit_local_recv_ts_ms"] - df["bybit_ts_ms"]
```

### Dual-read schema (v1 vs lean)

```python
import pyarrow.parquet as pq

schema_names = set(pq.read_schema(path).names)
if "spread_long" in schema_names:
    era = "v1"   # canary / legacy
else:
    era = "lean" # derive spreads
```

### Бары hive + join universe

```python
import pyarrow.dataset as ds
import pandas as pd

bars = ds.dataset(
    "research_data/bars/bar_5m",
    format="parquet",
    partitioning="hive",
).to_table(
    filter=(ds.field("base_coin") == "XRP")
).to_pandas()

universe = pd.read_csv("bybit_okx_universe.csv")
bars = bars.merge(universe, on="base_coin", how="left")
# volume = OKX volCcy (base); lot/tick — из CSV
```

### Hive тиков с live (только ops/срез, не SoT)

```python
import pyarrow.dataset as ds

ticks = ds.dataset(
    "/data/live",  # на VPS; на Mac — локальная копия hive
    format="parquet",
    partitioning="hive",
).to_table(filter=ds.field("base_coin") == "BTC")
```

---

## 9. Что НЕ использовать как вход модели

| Путь / артефакт | Почему |
|-----------------|--------|
| `/data/spool`, `.tmp`, `*.parquet.tmp` | незавершённая запись |
| `/data/live/archived/` | короткий retention; не архив исследования |
| `/data/compacted/sent/`, `/data/bars/sent/` | локальные копии после confirm; remote — канон |
| `output/lean_ticks`, `output/lean_bars` | локальный эксперимент `screaner_local_lean.py`, не прод |
| Логи ping / latency-эксперимент | ops-наблюдаемость, не ряд для бэктеста ([`docs/latency-screener-vs-ping-experiment.md`](latency-screener-vs-ping-experiment.md)) |
| Сумма L1 `_size` по тикам как «volume свечи» | другая величина; для гира 1.5 нужен слой `bar_5m` |
| Приватные каналы / `Trade_Lat` в parquet | снаружи симулятора |
| Смешение legacy-партиций **без** 8 book-колонок с полным L1 | гир 2+ / volume gate на стакане |

---

## See also

- [`docs/data-format-model.md`](data-format-model.md) — какие поля модель *просит*
- [`docs/storage-contract.md`](storage-contract.md) — канон writer
- [`docs/local-lean-collector.md`](local-lean-collector.md) — флаги lean/bars
- [`docs/bars-backup-20260805.md`](bars-backup-20260805.md) — появление bars на remote
- [`docs/compaction-backup-runbook.md`](compaction-backup-runbook.md) — lifecycle compact → backup
