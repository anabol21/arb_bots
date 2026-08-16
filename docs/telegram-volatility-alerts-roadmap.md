# Дорожная карта: Telegram-алерты аномальной волатильности

## Статус подпроекта

Ветка: `feat/telegram-volatility-alerts-v1`  
Аудитория v1: русскоязычные пользователи глобальных CEX.  
Продукт v1: только информационные Telegram-алерты.  
Торговля, private API, подписки с UI и персональные пороги — вне v1.

Основная спецификация: [`telegram-volatility-alerts-spec.md`](telegram-volatility-alerts-spec.md).  
Рыночный контекст: [`telegram-volatility-alerts-market-research.md`](telegram-volatility-alerts-market-research.md).
Provenance входных данных: [`telegram-alert-data-provenance.md`](telegram-alert-data-provenance.md).
Карта склейки скринера: [`telegram-alert-screener-sources.md`](telegram-alert-screener-sources.md).

## Зависимости старшего проекта

| Зависимость | Что требуется | Статус |
|------------|---------------|--------|
| D: published L1 ticks | Complete L1 для каузального mid-OHLC | Root и completeness фиксируются перед replay |
| D: `bar_5m` | Volume и своевременное закрытие окна | Root/freshness проверяются перед canary |
| M: гир 1.5 | Канонический regime score | Закрыт как historical screener; используется как versioned input contract |
| D: latency/устойчивость | Свежесть closed bars и сервисная наблюдаемость | Отдельная проверка alert canary |

Alert-сервис не разблокирует и не заменяет gate трека B из
[`program-roadmap.md`](program-roadmap.md).

## Этапы

### 0. Контракты и протокол разметки

**Owner:** Telegram Alert Orchestrator + Schema Contract + Model Simulator.  
**Статус:** `open`. Provenance published inputs зафиксирован; rubric,
эпизоды и locked holdout ещё не закрыты.

**Результат:** provenance published inputs, `t0`, cold-period, rubric ручной
разметки, versioned `manual_anomaly_label` и `persistence_30m`,
split/embargo, initial false-positive budget.

**Gate:** 30–50 независимых эпизодов; два разметчика на стратифицированной
подвыборке; locked holdout.

### 1. Offline rule baseline

**Owner:** Model Simulator.  
**Результат:** воспроизводимый replay опубликованных ticks/bars,
M 1.5-compatible score, rule-only policy и отчёт качества.

**Gate:** event-level precision не менее 80%, не более одного ложного
alert на asset-day на locked holdout, coverage и missing-data states
отражены явно.

### 2. Shadow ML persistence

**Owner:** Model Simulator → Integration Validator → Review Critic.  
**Результат:** `p_persist` для `persistence_30m`; baseline не изменён.

**Gate:** улучшение precision-recall над rule baseline на holdout,
калибровка (Brier/reliability), отсутствие leakage и псевдорепликации.

### 3. Alert contracts и replay service

**Owner:** Schema Contract → Alert Runtime & Delivery.  
**Результат:** versioned `RegimeObservation`, `AlertCandidate`,
`AlertOutbox`; детерминированный replay и accounting.

**Gate:** один входной набор создаёт один и тот же набор candidates;
missing data не скрывается fallback; candidate accounting сходится.

### 4. Telegram shadow canary

**Owner:** Alert Runtime & Delivery → Validation.  
**Результат:** outbox, idempotent Telegram adapter, закрытый recipient list,
метрики closed-bar-to-send.

**Gate:** `candidate = queued + sent + failed`, duplicate rate 0, p95
closed-bar-to-send ≤45 seconds, доказанные restart и Telegram outage paths.

### 5. Ограниченный production v1

**Owner:** Alert Runtime & Delivery → Validation → Review Critic.  
**Результат:** rule-only алерты на ограниченном universe ликвидных монет;
еженедельный review ложных alert и label drift.

**Gate:** заранее зафиксированное окно наблюдения без превышения
false-alert budget или необъяснённой delivery-loss.

## Следующие полезные возможности

После v1 и только по подтверждённому пользовательскому спросу:

1. Режимы «ранний» и «подтверждённый» с явно разной уверенностью.
2. Daily digest suppressed alerts и объяснимый cooldown.
3. Нейтральная карточка события с метриками, графиком и версиями данных.
4. Watchlist, тихие часы и профили чувствительности.
5. Дополнительные глобальные CEX после отдельной проверки data quality,
   задержки и контракта.

Не добавлять buy/sell рекомендации, автоторговлю, copy-trading, private
exchange APIs, гарантии доходности или платный broadcast до отдельного
продуктового и юридического решения.
