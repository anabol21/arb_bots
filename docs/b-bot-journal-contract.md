# Контракт журнала `B`-бота: `bbot.journal.v0`

## 1. Версия и область

Это контракт версии `v0` для журнала live stub-бота `app/bot`. Формат хранения — `JSONL`: одна строка содержит один JSON-объект одной ноги (`LEG`), а не одну spread-сделку.

Это отдельный журнал намерений и симулированных исполнений. Он не является `lean ticks`, `bar_5m` или иной существующей parquet-схемой и не меняет ни один из этих контрактов. Хранилище `v0` — только `JSONL`; parquet-схема в `app/schema` для него не создаётся.

На VPS допустим только следующий layout:

```text
/data/bbot/journal/event_date=YYYY-MM-DD/legs.jsonl
```

`event_date` обязателен и в hive-пути, и в теле каждой записи, чтобы строка была самодостаточно описана. Это календарная дата `UTC` из `signal_ts_ms` (Unix ms), вычисляемая только по формуле `datetime.utcfromtimestamp(signal_ts_ms / 1000).date().isoformat()` и записываемая как `YYYY-MM-DD`. Host-local timezone запрещён и для partition, и для поля `event_date`; дата в пути и теле обязана совпадать. Запрещены записи этого журнала в `/data/live`, `/data/bars`, `/data/compacted` и `/data/spool`.

Каждое dual-leg намерение создаёт ровно две записи с общим `intent_id`. Идентичность одной ноги задаётся сочетанием `intent_id` и `exchange`; дважды записанная одна и та же комбинация — ошибка журнала.

## 2. Producers и consumers

| Роль | Компонент | Назначение |
|---|---|---|
| Producer / writer | live stub-бот в `app/bot` (B Stub Runtime) | Создаёт строки намерения, stub-`place`/`ack`, заполнения или отмены. Это не collector. |
| Основной consumer | B Stub Validator | Проверяет инварианты строк, парность ног, задержку и отсутствие live-send. |
| Consumer | Validation Agent | Проверяет layout, наличие файлов только под `/data/bbot`, JSONL и полноту обязательных полей. |
| Consumer | operators | Читают журнал для операционного наблюдения и расследований. |
| Неосновной consumer | model notebook | Не является первичным consumer этого журнала; формат не объявлен входом модели. |

## 3. Текущая и целевая схема

Предыдущей версии нет: целевая схема впервые задаётся как `bbot.journal.v0`. Все приведённые ниже поля обязательны; отсутствие любого поля делает запись невалидной и должно отклоняться validator'ом fail-closed.

| Поле | JSON-тип | Единица / допустимые значения | Семантика |
|---|---|---|---|
| `schema_version` | string | константа `"bbot.journal.v0"` | Версия контракта строки. |
| `intent_id` | string | UUID либо монотонный ID, уникальный на намерение | Общий ID двух ног одного dual-leg намерения. |
| `base_coin` | string | например, `BTC` | Базовая монета намерения. |
| `exchange` | string | `"okx"` \| `"bybit"` | Биржа данной ноги. |
| `leg_side` | string | `"buy"` \| `"sell"` | Сторона именно биржевой ноги, не направление spread. |
| `spread_side` | string | `"open_long"` \| `"open_short"` \| `"close"` | Намерение policy. |
| `event_date` | string | `YYYY-MM-DD` | UTC-календарная дата `signal_ts_ms`: `datetime.utcfromtimestamp(signal_ts_ms / 1000).date().isoformat()`. Совпадает с датой в partition-пути; host-local timezone запрещён. |
| `signal_ts_ms` | number | Unix milliseconds | `event_local_ts_ms` signal tick, использованного policy. Не exchange timestamp и не время wall-clock записи. |
| `place_ts_ms` | number | Unix milliseconds | Когда stub выполнил `"would_send"`; не меньше `signal_ts_ms`. |
| `ack_ts_ms` | number | Unix milliseconds | Время stub-ack. Live ACK отсутствует; допустимо `ack_ts_ms = place_ts_ms`. |
| `fill_ts_ms` | number \| null | Unix milliseconds | Время следующего live valid tick с `ts >= signal_ts_ms + Trade_Lat_ms`; `null` для terminal `"aborted"`. Это не результат wall-clock sleep. |
| `Trade_Lat_ms` | number | milliseconds; в gear `1.0` константа `100` | Латентность исполнения; одинакова для обеих бирж. |
| `signal_price` | number | цена L1 | Цена L1 на signal tick для этой ноги: `ask` для `buy`, `bid` для `sell`. |
| `fill_price` | number \| null | цена L1 | Цена L1 соответствующей стороны на fill tick; `null`, если fill не произошёл. |
| `qty` | number | base quantity | Размер ноги в базовой валюте. |
| `notional` | number | quote notional | Номинал в котируемой валюте; у обеих ног одного `intent_id` одинаков. |
| `fee` | number | quote currency | Комиссия данной ноги: `fee_rate * notional`, где `fee_rate = 0.00075` согласно `gear 1.0 HYPER`. |
| `tick_valid` | boolean | `true` \| `false` | Обязательный признак прохождения fill tick fail-closed проверки skew/age/generation. При `fill_ts_ms = null` всегда `false`; при journal-статусе `"filled"` всегда `true`. |
| `suppress_reason` | string \| null | причина | Причина suppression, если применимо. |
| `status` | string | `"filled"` \| `"aborted"` | Неизменяемый terminal-статус строки в `legs.jsonl`. |
| `abort_reason` | string \| null | причина | Причина отмены; используется для `"aborted"`. |
| `would_send` | boolean | всегда `true` | Stub зарегистрировал действие, которое было бы отправлено. |
| `send` | boolean | всегда `false` в `v0` | Реальная отправка не допускается. |
| `k_live` | number | константа `1` | Лимит одновременных открытых намерений. |

Допускаются дополнительные поля, только если они не меняют значение обязательных:

- `okx_symbol`, `bybit_symbol`;
- ID либо цены snapshots `bid`/`ask` на signal и fill ticks, достаточные для восстановления выбранной цены;
- generation и validity flags signal tick наряду с аналогичными данными fill tick.

Полный enum состояния ноги вне журнала — `"pending"` \| `"acked"` \| `"filled"` \| `"aborted"`. Значения `"pending"` и `"acked"` существуют только во внутрипроцессном состоянии либо в `/data/bbot/state/`; они не являются строками `legs.jsonl` версии `v0`.

## 4. Совместимость и миграция

Поддерживается только `v0`. Версия определяется одновременно именем/назначением файла и обязательным `schema_version = "bbot.journal.v0"` в каждой строке. Consumer обязан явно отклонить незнакомую версию, а не подставлять значения по умолчанию.

`legs.jsonl` — append-only журнал неизменяемых terminal `LEG`-строк. Writer записывает ровно одну полную JSONL-строку для каждой пары `(intent_id, exchange)` и только при `status = "filled"` либо `status = "aborted"`; вторая строка с той же парой — ошибка журнала. Строки `"pending"` и `"acked"` не append'ятся: in-flight состояние остаётся в `/data/bbot/state/`, чья схема данным контрактом не определяется.

При crash частичная последняя строка не является записью и никогда не должна приниматься reader'ом за валидный JSON. Writer обязан дописывать только полную строку через временную строку с последующим `flush`; reader/validator обязан явно отклонить повреждённую частичную строку, не восстанавливая и не достраивая её. Миграции предыдущих данных нет, поскольку предыдущей версии не существует.

## 5. Риски тихой несовместимости

- Запись одной строки на spread-сделку вместо двух `LEG`-строк разрушит сопоставление сторон и проверку одинакового `notional`.
- Использование exchange timestamp или wall-clock времени вместо `event_local_ts_ms` для `signal_ts_ms` изменит семантику `Trade_Lat_ms` без смены типов.
- Вычисление `event_date` в host-local timezone либо несовпадение даты в пути и теле строки помещает terminal-ногу в неверную UTC-partition.
- Заполнение по sleep, а не по следующему live valid tick, создаст видимость исполнения без рыночного основания.
- `tick_valid = false` вместе с `"filled"`, `tick_valid = true` при `fill_ts_ms = null`, либо fill на stale/suppressed tick нарушает fail-closed правило.
- Append `"pending"`/`"acked"` либо повторная строка `(intent_id, exchange)` делает terminal-журнал неоднозначным; частичная crash-строка не может считаться записью.
- Значения `would_send = false`, `send = true`, live order IDs, API keys или private URLs превращают stub-журнал в иной и небезопасный контракт.
- Отсутствие одного обязательного поля, подстановка `null` вместо требуемого значения или неполная последняя строка могут выглядеть как читаемый JSONL, но должны быть отклонены.
- Размещение журнала в деревьях `D` смешает независимые storage boundary и может быть ошибочно принято за данные collector'а.
- Нарушение `k_live = 1` — два открытых intent одновременно — не исправляется чтением журнала задним числом и должно выявляться validator'ом.

## 6. Передача реализации по агентам

- B Stub Runtime реализует writer в `app/bot`, создаёт две leg-записи на intent, сохраняет только этот JSONL-контракт и гарантирует `would_send = true`, `send = false`.
- B Stub Validator проверяет обязательные поля, типы, значения-константы, две ноги на `intent_id`, пары `okx`/`bybit`, общий `notional`, правила времени fill и `k_live = 1`.
- Validation Agent проверяет, что журнал существует и материализуется только под `/data/bbot/journal/event_date=YYYY-MM-DD/legs.jsonl`.
- Runtime Storage не переносит этот журнал в деревья `D` и не записывает его в `/data/live`, `/data/bars`, `/data/compacted` или `/data/spool`.

Ни один из этих шагов не требует изменения `app/schema/*`, существующих parquet-контрактов или collector'а.

## 7. Проверки и критерии приёмки

1. `legs.jsonl` содержит только полные неизменяемые terminal `LEG`-строки; каждая непустая строка парсится как один JSON object. Частичная последняя crash-строка не является записью, не принимается reader'ом как валидная и даёт validator'у явную ошибку.
2. В каждой записи присутствуют все обязательные поля, JSON-типы совпадают с таблицей, `schema_version` равен `"bbot.journal.v0"`.
3. `event_date` в пути и теле совпадает и равен UTC-календарной дате `signal_ts_ms` по `datetime.utcfromtimestamp(signal_ts_ms / 1000).date().isoformat()`; host-local timezone не используется. Layout не создаётся вне `/data/bbot/journal/`.
4. Для каждого `intent_id` есть ровно две terminal-записи, по одной для `okx` и `bybit`; их `notional` одинаков. Для каждой пары `(intent_id, exchange)` существует ровно одна строка.
5. Всегда выполняется `would_send is true`, `send is false`, `k_live == 1`, `place_ts_ms >= signal_ts_ms`.
6. В `legs.jsonl` `status` допускает только `"filled"` либо `"aborted"`; `"pending"` и `"acked"` остаются только in-memory либо в `/data/bbot/state/` и не имеют JSONL-строк.
7. Для `"filled"`: `fill_ts_ms` и `fill_price` не `null`, `tick_valid is true`, а `fill_ts_ms >= signal_ts_ms + Trade_Lat_ms`.
8. При `fill_ts_ms is null`, включая terminal `"aborted"`, `tick_valid is false`; для stale/suppressed или невалидного tick fill отсутствует, а `fill_ts_ms` и `fill_price` равны `null`.
9. В строках нет API keys, live order IDs и private URLs.

## 8. Следующий шаг

Передать этот документ B Stub Runtime для узкой реализации JSONL writer в `app/bot`, затем передать независимые проверки B Stub Validator и Validation Agent. До появления их проверки контракт остаётся спецификацией, а не доказательством корректности VPS-хранения.
