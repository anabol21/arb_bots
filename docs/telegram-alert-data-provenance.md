# Provenance входных данных Telegram-алертов

## Статус и границы

Этот документ фиксирует входной контракт alert-only consumer. Этап 0
остаётся открытым: provenance и правила отклонения описаны, а rubric,
эпизоды, два разметчика и locked holdout ещё не закрыты. Документ не
меняет collector, storage lifecycle или модель сделок и не является
доказательством готовности Telegram canary.

Потребитель будущего `alerts/` читает только опубликованные данные. Решение
принимается на закрытом 5m окне и не использует incomplete parquet, future
bars или private API.

## Producer → consumer

| Вход | Producer / контракт | Consumer | Обязательное условие |
|------|----------------------|----------|----------------------|
| Complete L1 | `app/screaner_b_o.py` → lean ticks, [`lean_event.py`](../app/schema/lean_event.py) | Mid-OHLC builder | 8 L1 book-полей, UTC timestamps, один `base_coin` |
| Closed `bar_5m` | `app/screaner_b_o.py` → `bar_5m`, [`lean_event.py`](../app/schema/lean_event.py) | Volume feature | `bar_start_ts_ms`, `bar_end_ts_ms`, `base_coin`, `ref_exchange`, `volume`; окно закрыто |
| Regime metric | [`regime_ma_ratio.py`](../research/regime_ma_ratio.py), [`regime-metrics-v0.md`](regime-metrics-v0.md) | Rule-only replay / service | versioned M 1.5-compatible score и его компоненты |

`bar_5m` содержит только `volume`. Mid-OHLC и ATR-like amplitude строятся
каузально из complete L1, а не подменяются OHLC внешнего REST-дампа.
`spread_*`, latency и freshness вычисляются при чтении, если их требует
policy; они не являются обязательными колонками lean body.

## Выбор источника по режиму

| Режим | Допустимый root | Назначение | Нельзя утверждать |
|-------|-----------------|------------|-------------------|
| Offline replay | Один versioned historical input manifest с точным root и schema era | Воспроизводимая оценка policy | Что набор свежий или production-like без отдельной проверки |
| VPS shadow / canary | Published VPS-local source, выбранный и проверенный до запуска | Свежие closed windows для решения и SLO | Что это durable remote history |
| Historical research | Изолированный REST OHLC+volume dump | Калибровка и ручная разметка | Что он заменяет live `bar_5m` или доказывает delivery latency |

На pre-B снимке 2026-08-16 ticks считаются durable только после confirmed
remote transfer в `backup1tb:spread-compacted`; bars считаются VPS-local
в `/data/bars`.
Remote bars-backup и compacted-bars v2 не являются автоматически свежим или
активным источником alert consumer. Перед каждым replay/canary runner обязан
записать фактически выбранный root и observed freshness.

## Совместимость и явное отклонение

| Вход | Принимается | Отклоняется с `missing_data` |
|------|-------------|------------------------------|
| Ticks | Один schema era за input manifest; lean с полным L1 либо v1 с явно проверенной схемой | Legacy без всех 8 book-полей; v1 и lean в одном невыравненном чтении; temporary/spool files |
| Bars | Один root, ровно `LEAN_BAR_5M_BODY_COLS`; canonical volume — `ref_exchange == "okx"`; identity `base_coin + ref_exchange + bar_start_ts_ms` | Union source и compacted roots; duplicate identity; `ref_exchange` кроме `okx` без явного versioned opt-in; open/partial bar |
| Coin/window | L1, volume и cross-exchange evidence согласованы с одним closed window | Недостаточный 48-bar warmup, gap, skew или нет подтверждения второй биржи |

Отсутствующие и несовместимые входы не получают синтетических значений: они
должны попасть в versioned `missing_data` / suppression accounting будущего
`AlertCandidate`.

## Evidence перед следующими этапами

Для каждого offline replay фиксируются: Git revision, metric version, input
manifest, root, schema era, UTC time range, число монет/окон, completeness и
исключённые окна. Для VPS canary дополнительно фиксируются: command, log path,
first materialization path, observed close-to-read lag, backlog и состояние
collector.

Проверка этого документа: Schema Contract Agent подтверждает schema/root
правила; Validation Agent проверяет реальный published sample перед canary;
Integration Validator проверяет детерминированное чтение того же manifest
replay.
