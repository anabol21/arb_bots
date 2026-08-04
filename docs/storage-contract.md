# Контракт хранения спредов (трек сбора)

Единый список колонок тела parquet и раскладки партиций. Источник правды в коде: [`app/schema/spread_event.py`](../app/schema/spread_event.py), [`app/schema/parquet_layout.py`](../app/schema/parquet_layout.py).

Взгляд модели (гир 1–2, legacy): [`docs/data-format-model.md`](data-format-model.md).

---

## Путь на диске

```text
<SPREADS_ROOT>/base_coin=<COIN>/event_date=<YYYY-MM-DD>/<batch_or_part>.parquet
```

- `base_coin`, `event_date` — только hive-партиции.
- В теле файла колонки партиций **нет** (`event_date` отбрасывается перед записью).

Рантайм-вход: `app/screaner_b_o.py` → нормализация/запись: `app/storage/writer.py`.

---

## Body-колонки (полный контракт v1)

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

Производные при нормализации:

- `okx_freshness_ms` = `calc_local_ts_ms − okx_local_recv_ts_ms` (аналогично Bybit)
- `event_local_ts_ms` = recv триггера (`okx` или `bybit` по `trigger`)
- `max_freshness_ms` / `max_latency_ms` = max по двум биржам

L1 book: публичный лучший bid/ask (цена + размер). Суффикс объёма — `_size`.

---

## Вне контракта v1

- Приватные каналы (latency ордера, аккаунт).
- L2+, trades tape, funding, OI.
- Переименование `_size` → `_quantity`.

---

## Legacy

Файлы без 8 book-колонок допустимы как исторический хвост, но **не** как целевой продакшен-формат. Проверка бэкапа: [`validation/check_backup_validity.py`](../validation/check_backup_validity.py) ожидает полный `EXPECTED_BODY_COLS` (= body выше).

---

## Версия

**v1** — замороженный полный body **тиков**. Любое урезание колонок — новая версия контракта + миграция валидации.

### Кандидат v1.1 — слой баров `5m` (запрос модели)

Не часть тикового body. Со стороны модели — запрос ряда объёма свечи `5m` для **скринера режима (гир 1.5)**: `bar_*_ts`, `base_coin`, `ref_exchange`, `volume`. Спреды и амплитуда **не** в барах: считаются из L1 тиков в модели. Семантика `volume` — у сборщика (подписка на канал баров), не в этом контракте. Подробности запроса: [`docs/data-format-model.md`](data-format-model.md). Лестница гиров: [`docs/strategy-gears.md`](strategy-gears.md).

Gap ingest ↔ модель (lean schema, store vs derive, миграция): [`docs/data-format-ingest-gap.md`](data-format-ingest-gap.md).

Тиковые `spread_long` / `spread_short`: в v1 ещё могут писаться; для модели целевой путь — считать из L1, не расширять и не дублировать.
