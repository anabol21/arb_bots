# Контракт приватного журнала B: `bbot.private.journal.v1`

## 1. Область и границы

Трек: Glue / private execution. Pipeline block: приватный адаптер → append-only
операционный журнал. Владелец спецификации: Schema Contract Agent.

Этот документ определяет **только** формат событий. Он не реализует auth,
account read, отправку, отмену или маршрутизацию ордеров и не является
свидетельством их выполнения.

Журнал версии `v1` существует только в отдельном дереве:

```text
/data/bbot/private/journal/event_date=YYYY-MM-DD/events.jsonl
```

`event_date` — UTC-дата поля `event_ts_utc`, в формате `YYYY-MM-DD`; дата в
пути и в объекте обязана совпадать. Локальный dev fallback, если он разрешён
runtime, должен сохранять тот же относительный layout под
`output/bbot/private/`, но не меняет VPS-цель.

Это не stub-журнал `bbot.journal.v0`: private writer не пишет в
`/data/bbot/journal/`, а stub writer не пишет в `/data/bbot/private/`.
Формат не является parquet, входом collector'а или контрактом `lean ticks`.
Запрещены любые записи private-журнала в корни D:

```text
/data/live
/data/bars
/data/compacted
/data/spool
```

## 2. JSONL и append-only правила

Каждая непустая строка `events.jsonl` — один законченный JSON object с
`schema_version = "bbot.private.journal.v1"`. Файл append-only: существующая
строка не перезаписывается, не удаляется и не меняется. Новое наблюдение,
исправление либо результат reconciliation добавляется новым событием, а не
правкой старого.

Writer добавляет только целую UTF-8 JSONL-строку, завершая её `\n`. Неполная
последняя crash-строка не является событием: reader и validator должны
отклонить файл с явной ошибкой, а не угадывать или восстанавливать поля.
Неизвестные версии, event types и поля вне разрешённых настоящим контрактом
отклоняются fail-closed.

## 3. Обязательные общие поля

Каждое событие обязательно содержит все поля ниже. Они образуют корреляционную
основу и не могут быть `null`.

| Поле | Тип / допустимые значения | Значение |
|---|---|---|
| `schema_version` | string, ровно `"bbot.private.journal.v1"` | Версия строки. |
| `event_id` | string, UUID/ULID | Уникален в журнале. |
| `event_type` | один из §4 | Тип события. |
| `event_date` | string `YYYY-MM-DD` | UTC-дата `event_ts_utc`; совпадает с partition. |
| `event_ts_utc` | RFC 3339 UTC string с суффиксом `Z` | Wall-clock время наблюдения/записи события. |
| `event_monotonic_ns` | integer ≥ 0 | Монотонное время процесса в ns; не Unix epoch. |
| `run_id` | string UUID/ULID | Уникальный запуск процесса. |
| `operation_id` | string UUID/ULID | Одна связанная операция: auth, account read либо lifecycle одной ноги. |
| `event_seq` | integer ≥ 1 | Строго возрастающий номер события внутри `run_id`. |
| `venue` | `"bybit"` \| `"okx"` | Площадка события. |
| `environment` | `"testnet"` \| `"demo"` \| `"live"` | Среда именно этой площадки. |
| `outcome` | `"success"` \| `"failure"` \| `"pending"` \| `"observed"` | Результат, известный на момент append. |

`operation_id` не является user/account/order identifier и не должен
производиться из него. Для future order lifecycle `operation_id` одинаков у
`order_prepared`, `request_sent`, `ack_received`, terminal/cancel/reject
событий этой ноги. Для событий двух ног связь задаётся разрешённым полем
`dual_leg_id`, а не общим или реальным биржевым ID.

## 4. Event types и разрешённые дополнительные поля

Кроме обязательных общих полей разрешены **только** поля из строки
соответствующего `event_type`. Все ID в таблице — сгенерированные приложением
opaque IDs; они не могут содержать API key, signature, account identifier,
client order ID или exchange order ID.

| `event_type` | Допустимый `outcome` | Назначение | Разрешённые дополнительные поля |
|---|---|---|---|
| `auth` | `"success"` \| `"failure"` | Итог одной auth-попытки без секретов | `auth_method`, `credential_presence`, `error_code` |
| `account_read` | `"success"` \| `"failure"` | Итог read-only account request без значений счёта | `account_scope`, `request_kind`, `error_code` |
| `operator_approval` | `"success"` \| `"observed"` | Каноническая выдача либо одноразовое потребление operator approval | `approval_action`, `approval_token_fingerprint`, `approval_scope`, `approval_record_id`, `approval_expires_at_utc`, `approval_grant_event_id`, `consumed_for_operation_id` |
| `pre_send_gate` | `"observed"` | Non-order блокировка rest/price gate до transport dispatch | `gate_kind`, `gate_decision` |
| `order_prepared` | `"pending"` | Будущее намерение одной ноги, до сетевой отправки | `dual_leg_id`, `leg_id`, `instrument_class`, `symbol_alias`, `side`, `order_kind`, `quantity_bucket`, `notional_bucket`, `reduce_only`, `post_only`, `ttl_bucket`, `request_fingerprint` |
| `request_sent` | `"pending"` \| `"failure"` | Начат dispatch либо служебный WS request; исход неизвестен до ACK либо reconciliation | `dual_leg_id`, `leg_id`, `request_kind`, `request_fingerprint`, `transport_attempt`, `send_monotonic_ns`, `transport`, `reconnect_generation`, `subscription_readiness`, `error_code` |
| `ack_received` | `"success"` \| `"failure"` | Получен ответ-подтверждение transport/venue либо WS subscription | `dual_leg_id`, `leg_id`, `request_kind`, `request_fingerprint`, `ack_state`, `receive_monotonic_ns`, `transport`, `reconnect_generation`, `subscription_readiness`, `error_code` |
| `terminal_update` | `"observed"` | Наблюдён terminal state ордера; private WS update не заменяет trade ACK | `dual_leg_id`, `leg_id`, `terminal_state`, `request_fingerprint`, `receive_monotonic_ns`, `exchange_event_ts_utc`, `clock_offset_evidence`, `observation_source`, `reconnect_generation`, `sequence_state` |
| `cancel_requested` | `"pending"` \| `"failure"` | Подготовлена или отправлена отмена уже acked ноги | `dual_leg_id`, `leg_id`, `request_fingerprint`, `cancel_reason`, `send_monotonic_ns`, `error_code` |
| `cancel_ack` | `"success"` \| `"failure"` | Подтверждение cancel | `dual_leg_id`, `leg_id`, `cancel_state`, `request_fingerprint`, `receive_monotonic_ns`, `error_code` |
| `reject` | `"failure"` | Venue/transport отклонил auth, запрос или действие | `dual_leg_id`, `leg_id`, `request_kind`, `request_fingerprint`, `reject_stage`, `error_code` |
| `dual_leg_abort` | `"observed"` | Вторая нога прекращена вследствие состояния первой | `dual_leg_id`, `leg_id`, `peer_leg_id`, `abort_reason`, `request_fingerprint` |
| `reconciliation` | `"success"` \| `"failure"` \| `"observed"` | Сверка локального lifecycle либо состояния private stream с разрешённым безопасным итогом | `dual_leg_id`, `leg_id`, `reconciliation_scope`, `reconciliation_state`, `mismatch_fields`, `transport`, `observation_source`, `reconnect_generation`, `sequence_state`, `subscription_readiness`, `error_code` |
| `latency_summary` | `"observed"` | Агрегат измерений одного `operation_id` | `dual_leg_id`, `leg_id`, `latency_intervals_ms`, `clock_offset_evidence`, `latency_basis`, `sample_count` |

Типы значений:

- Для `auth` поле `credential_presence` обязательно и принимает **ровно одну**
  из двух форм без дополнительных ключей:
  `{"api_key_present": boolean, "api_secret_present": boolean, "passphrase_present": boolean}`
  (legacy v1) **либо** `{"credentials_configured": boolean}` (current v1).
  В обеих формах допускаются только boolean, никогда credential values, names
  либо identifiers. Новый writer обязан создавать current-форму; validator
  обязан принимать обе формы, чтобы historical append-only records не
  переписывались.
  `auth_method`: `"hmac"` \| `"api_key_signature"`.
- `account_scope`: `"wallet"` \| `"balance"` \| `"positions"`; `request_kind`:
  `"account_read"` \| `"place"` \| `"cancel"` \| `"ws_subscribe"`. `ws_subscribe`
  — только служебная подписка private execution stream, не account read и не
  ордерный request.
- `approval_action`: `"granted"` \| `"consumed"`; `approval_scope`:
  `"live_order_send"`; `approval_record_id` — opaque ID записи approval.
  `approval_token_fingerprint` — HMAC-SHA-256 с domain separator
  `"bbot.private.approval.v1"` и приватным runtime key, закодированный
  64-character lowercase hex; это **не** token и не raw operator phrase. `approval_expires_at_utc`
  обязателен только при `"granted"`; `approval_grant_event_id` и
  `consumed_for_operation_id` обязательны только при `"consumed"`.
  `approval_grant_event_id` ссылается на ранее append'нутое `"granted"` событие
  с теми же `approval_record_id`, `approval_token_fingerprint` и
  `approval_scope`; `consumed_for_operation_id` — будущий order `operation_id`.
- `gate_kind`: `"rest"` \| `"price"`; `gate_decision`: `"blocked"`.
  `pre_send_gate` не несёт order, price, size, payload или venue-state данных.
- `instrument_class`: `"spot"` \| `"linear_perpetual"` \| `"inverse_perpetual"`;
  `symbol_alias` — статический alias, например `"BTC-USDT-SWAP"`, без account
  или order ID; `side`: `"buy"` \| `"sell"`; `order_kind`: `"market"` \|
  `"limit"`; `quantity_bucket` и `notional_bucket` — только заранее
  определённые строки-бакеты, не числа/цены; `reduce_only`, `post_only` —
  boolean. `ttl_bucket`: `"short"` \| `"medium"` \| `"long"`, обязателен
  только при `post_only = true`; конкретный TTL и deadline в журнал не пишутся.
- `request_fingerprint` — односторонний безопасный fingerprint приложения,
  не включающий raw request, canonical signing string или secret-derived
  material; `transport_attempt`, `sample_count` — integers ≥ 1.
- `transport`: `"ws_trade"` \| `"rest"`. Новый WS-aware writer обязан
  указывать его для `request_sent`/`ack_received` с
  `request_kind = "place"|"cancel"` и для private-stream `reconciliation` с
  `observation_source = "rest_reconcile"`; `ws_trade` разрешён также только
  для `request_sent`/`ack_received` с `request_kind = "ws_subscribe"`. Для
  private WS gap/reconnect observation `transport` запрещён: это не command
  transport.
  Поле описывает транспорт команды, а не источник наблюдения terminal state.
- `observation_source`: `"private_ws"` \| `"rest_reconcile"`. Оно обязательно
  для `terminal_update` и private-stream `reconciliation`: terminal update
  private stream имеет ровно `"private_ws"`; REST reseed/reconciliation имеет
  ровно `"rest_reconcile"`. Raw WS frames, REST response и их identifiers не
  сериализуются.
- `reconnect_generation` — integer ≥ 0, счётчик соединений private WS внутри
  одного `run_id`/`venue`/`environment`; первое успешно залогиненное
  соединение имеет `0`, каждое следующее — предыдущий счётчик +1. Он обязателен
  для WS subscription, private WS terminal observation и private-stream
  reconciliation, запрещён для чистого REST order lifecycle.
- `sequence_state`: `"healthy"` \| `"gap"` \| `"reseed_required"`.
  Обязателен для private WS `terminal_update` и private-stream
  `reconciliation`; `"gap"`/`"reseed_required"` не являются terminal state
  ордера. `subscription_readiness`: `"not_ready"` \| `"ready"`; обязателен
  для `request_sent`/`ack_received` с `request_kind = "ws_subscribe"` и для
  private-stream `reconciliation`. `"ready"` допускается в successful
  subscription ACK; новые sends разрешены только после successful REST reseed
  текущей generation.
- `latency_intervals_ms` — object, где ключи являются только именами из §5, а
  значения — числа milliseconds ≥ 0; `latency_basis`:
  `"monotonic_local"` \| `"offset_adjusted_observed"`.
- `ack_state`: `"accepted"` \| `"received"`; `terminal_state`:
  `"filled"` \| `"cancelled"` \| `"expired"`; `cancel_state`:
  `"accepted"` \| `"cancelled"`; `reject_stage`: `"auth"` \| `"prepare"` \|
  `"send"` \| `"ack"` \| `"cancel"`.
- `cancel_reason`: `"operator_request"` \| `"timeout_guard"` \|
  `"dual_leg_guard"` \| `"post_only_ttl_expired"`; `abort_reason`:
  `"peer_rejected"` \| `"peer_timeout"` \|
  `"peer_terminal_before_send"` \| `"safety_guard"`.
- `reconciliation_scope`: `"order_state"` \| `"request_ack"` \|
  `"dual_leg_state"` \| `"post_dispatch_ambiguity"` \|
  `"post_only_ttl_recovery"` \| `"private_stream_reseed"`; `reconciliation_state`: `"matched"` \|
  `"mismatch"` \| `"inconclusive"`; `mismatch_fields` — array из `"state"` \|
  `"timing"` \| `"fingerprint"` \| `"leg_link"`.

`error_code` обязателен, если `outcome = "failure"`, и запрещён при
`outcome = "success"`; при `"pending"`/`"observed"` он отсутствует. Допустим
только точный allowlist:

```text
auth_failed
auth_unavailable
account_read_failed
invalid_request
signature_error
network_error
timeout
transport_error
rate_limited
venue_rejected
order_rejected
cancel_rejected
reconciliation_mismatch
dual_leg_aborted
internal_error
unknown
```

Нельзя сериализовать текст exception, response body или произвольное сообщение
ошибки вместо `error_code`.

### 4.0 Auth compatibility и migration

Это уточнение не создаёт `schema_version` v2 и не разрешает rewrite, delete,
replace или backfill historical JSONL records. Validator v1 обязан валидировать
каждый существующий `auth` event по одной из двух точных
`credential_presence` форм выше; hybrid object, missing key, `null`, string,
number, value/identifier либо дополнительный ключ — invalid.

Migration означает только обновление validator contract: legacy records
остаются append-only на месте, а новые auth events используют current-форму.
При обнаружении невалидной historical строки validator сообщает failure для
этой строки; он не исправляет, не удаляет и не создаёт заменяющее событие.

Добавленные WS-observability поля (`transport`, `observation_source`,
`reconnect_generation`, `sequence_state`, `subscription_readiness`) опциональны
для существующих v1 order-lifecycle строк: их отсутствие не меняет historical
семантику и не требует backfill. Новый writer обязан выполнять условия
применимости из §4 и §5.1; validator принимает отсутствие этих полей только
как legacy-compatible v1 representation, а при наличии проверяет точный
allowlist, тип и последовательность.

### 4.1 Canonical operator approval и одноразовое consumption

`operator_approval` — единственный канонический event type для разрешения и
потребления approval. Запрещены sibling JSONL, sidecar-файлы, state-файлы или
нежурналируемая memory-only отметка как источник истины об approval/consumption.

Событие с `approval_action = "granted"` создаёт доступный approval record.
Его `operation_id` относится только к выдаче approval. Событие с
`approval_action = "consumed"` имеет `operation_id`, **равный**
`consumed_for_operation_id`: это заранее созданный `operation_id` одной
ордерной ноги. Поэтому consumer может связать consumption с последующими
`order_prepared` и `request_sent`, не храня operator identity, wording или
token.

Один `approval_record_id` и один `approval_token_fingerprint` могут иметь
ровно одно `"granted"` и не более одного `"consumed"` события. Consumption
разрешён только до `approval_expires_at_utc` grant-события; оно не может быть
отменено, повторно использовано или «восстановлено» новым событием. Событие
`"consumed"` должно быть append'нуто и durably flushed в **этом же**
`events.jsonl` до первого `request_sent` связанного `operation_id`.

До append любого live `order_prepared` writer обязан выполнить offline
stream-validation canonical v1 журнала и принять связанный `"consumed"`:
ровно один неистёкший grant, совпадающие scope/record/fingerprint, отсутствие
предыдущего consumption и требуемый canonical порядок событий. Недостаточно наличия
строки `"consumed"` в памяти, отдельном файле или текущей partition.

Проверка доступности, запись `"consumed"` и durable append выполняются одним
writer под **глобальным** эксклюзивным межпроцессным lock
`/data/bbot/private/journal/.approval.lock`, а не lock одной UTC-partition.
Lock охватывает read всех canonical v1 событий данного fingerprint, решение
«ещё не consumed», append строки и flush/sync перед release. Это устраняет
двойное consumption на границе UTC-даты.

В journal tree единственный допустимый persistent non-event filesystem
artifact — этот `.lock`; он содержит только lock primitive и не является
состоянием approval, lease или recovery. Допустимы сами
`event_date=YYYY-MM-DD/` directories и `events.jsonl`; запрещены любые
sidecar JSONL, включая `post_only_leases.jsonl`, consumption/state JSON,
database, marker, cache или recovery-файл. Несколько writers, отдельный файл
consumption или отправка до durable canonical append запрещены. Crash до
durable append означает «не consumed» и не разрешает send; crash после него
означает «consumed», даже если send не произошёл.

### 4.2 Reconciliation для неопределённого dispatch и post-only TTL

`request_sent` означает только, что начат локальный вызов dispatch. Он
**никогда** не доказывает, что venue получил, принял или создал ордер. Если
после него не получен достоверный ACK (timeout, connection break, process
restart), исход dispatch неизвестен. Это не `reject` и не `terminal_update`:
appendится `reconciliation` с
`reconciliation_scope = "post_dispatch_ambiguity"` и
`reconciliation_state = "inconclusive"`, пока безопасная сверка не даст
`"matched"` либо `"mismatch"`. Последующая сверка описывает только безопасный
категориальный результат через уже разрешённые `reconciliation_state` и
`mismatch_fields`; raw venue response, order/account ID, values или payload не
добавляются.

При restart consumer обязан offline пройти весь canonical stream. Для каждого
`request_sent`, за которым нет `ack_received`, `reject`, `terminal_update` или
предшествующей финальной reconciliation, он обязан append'нуть
`reconciliation(post_dispatch_ambiguity, inconclusive)` **до** любой новой
попытки этой ноги, нового terminal/reject claim или завершения recovery.
Состояние остаётся неопределённым до последующей reconciliation; нельзя
синтезировать ACK, reject либо terminal из одного journal record.

Для `order_prepared.post_only = true` lease и recovery state существуют только
как canonical последовательность `order_prepared` → `cancel_requested` →
`cancel_ack`/`terminal_update` → `reconciliation`; отдельного lease state нет.
Runtime обязан иметь локальную TTL политику, но её точный deadline не
журналируется. Если до истечения TTL нет наблюдённого `terminal_update`,
обязателен `cancel_requested` с
`cancel_reason = "post_only_ttl_expired"`, затем `cancel_ack` и
`terminal_update`, когда они наблюдаются. Recovery evidence appendится как
`reconciliation` с `reconciliation_scope = "post_only_ttl_recovery"`:
`"matched"` допустим только после наблюдённой terminal/cancel
последовательности, `"inconclusive"` сохраняет незавершённость, а `"mismatch"`
требует `error_code = "reconciliation_mismatch"`. Ни cancel, ни
reconciliation не означают отсутствия позиции без соответствующего
наблюдённого terminal/reconciliation evidence.

### 4.3 Pre-send rest/price gate

Канонический v1 encoding для rest/price gate — `pre_send_gate` с
`gate_decision = "blocked"`. Это non-order event: он может быть append'нут
после policy/approval проверки, но **до** `order_prepared` и всегда до
`request_sent`. Он не является `reject`, `dual_leg_abort`, `terminal_update`
или `reconciliation` и не создаёт order lifecycle.

Для заблокированной gate не append'ятся `request_sent`, `ack_received`,
`terminal_update`, `cancel_requested`, `cancel_ack` или
`reconciliation(order_state|request_ack|post_dispatch_ambiguity)`. В
частности, order-state reconciliation запрещён, когда transport dispatch
никогда не был начат. `reject` с `reject_stage = "prepare"` остаётся только
для валидно созданного `order_prepared` и локальной подготовки запроса; его
нельзя использовать как замену `pre_send_gate`.

#### Узкое historical исключение `legacy_pre_send_no_dispatch`

Только для уже append'нутых v1 строк validator может признать
`legacy_pre_send_no_dispatch` и **семантически supersede** его как
`pre_send_gate(blocked)` без записи, удаления либо изменения JSONL. Исключение
применимо лишь при всех условиях одновременно для одного `operation_id`:

1. есть ровно один `reject` с `outcome = "failure"`,
   `reject_stage = "auth"` и `error_code = "invalid_request"`;
2. непосредственно после него в том же operation идёт ровно один
   `reconciliation` с `reconciliation_scope = "order_state"` и
   `reconciliation_state = "inconclusive"` и `outcome = "observed"`;
3. это единственные два события данного `operation_id`: отсутствуют
   `order_prepared`, `request_sent`, `ack_received`, `terminal_update`,
   `cancel_requested`, `cancel_ack`, `dual_leg_abort`, `latency_summary` и
   любые другие event types/reconciliation;
4. в обеих строках отсутствуют order ID любого вида.

Это migration-only read interpretation, а не новый writer encoding: новые
pre-send rest/price block используют только `pre_send_gate`. Любой
`reconciliation(order_state, inconclusive)` без `request_sent`, который не
совпадает со всеми четырьмя условиями, должен быть отклонён validator'ом
fail-closed.

## 5. Время, хронология и интервалы

`event_ts_utc` нужен для сопоставления процессов, а `event_monotonic_ns` —
для длительностей внутри одного `run_id`. UTC timestamp обязан иметь `Z`;
`event_date` вычисляется из него. Для любого run `event_seq` и
`event_monotonic_ns` строго возрастают, а `event_ts_utc` не убывает. Между
разными `run_id` монотонные значения не сравниваются.

Дополнительные `send_monotonic_ns` и `receive_monotonic_ns`, когда разрешены,
должны принадлежать текущему `run_id`, быть ≥ 0 и не противоречить смыслу
события. В частности, `send_monotonic_ns` в `request_sent` не больше
`event_monotonic_ns`, а `receive_monotonic_ns` в `ack_received`,
`terminal_update` и `cancel_ack` не больше `event_monotonic_ns`.

Допустимый порядок для `operation_id` будущей ордерной ноги:

```text
operator_approval(consumed) →
  (pre_send_gate(blocked) |
   order_prepared → request_sent → ack_received →
     (terminal_update | cancel_requested → cancel_ack → terminal_update))
```

`reject` может завершить order lifecycle после `order_prepared`, `request_sent`,
`ack_received` или `cancel_requested` согласно `reject_stage`. `dual_leg_abort`
ссылается на существующий `dual_leg_id` и не может быть раньше наблюдения,
которое его вызвало. `reconciliation` и `latency_summary` append'ятся только
после событий, которые они описывают. Повторный terminal state не заменяет
первый: он допустим только как отдельное `reconciliation` с явным
`reconciliation_state`. Исключение для testnet/demo read-only либо этапов без
send: `operator_approval` отсутствует; если send разрешён соответствующей
политикой, `"consumed"` обязателен непосредственно перед lifecycle.

### 5.1 Private WS execution observability

Расширение использует существующие `auth`, `request_sent`, `ack_received`,
`terminal_update` и `reconciliation`; нового event type не создаёт. Для одной
private WS generation допустима только последовательность:

```text
auth(success) →
request_sent(ws_subscribe, ws_trade, not_ready) →
ack_received(ws_subscribe, ws_trade, ready) →
reconciliation(private_stream_reseed, rest_reconcile, rest, healthy, ready)
```

`auth(success)` фиксирует только успешный WS login без credentials, raw frames
или venue/session identifiers. `request_sent` и `ack_received` для
`ws_subscribe` принадлежат stream operation, не order leg: у них запрещены
`dual_leg_id`, `leg_id` и `request_fingerprint`. Для `ws_subscribe`
`ack_state = "received"` означает только observed subscription ACK.

После readiness/reseed `request_sent(place|cancel, transport=ws_trade)` может
получить `ack_received`. Такой WS trade ACK фиксируется как
`ack_received(ack_state=accepted|received)` и **никогда не является
terminal_update**, даже если venue в ACK сообщает состояние. Terminal outcome
добавляется исключительно отдельным `terminal_update` с
`observation_source = "private_ws"` и `sequence_state = "healthy"`.

При reconnect либо обнаруженном sequence gap writer append'ит
`reconciliation(private_stream_reseed, outcome=observed,
observation_source=private_ws, sequence_state=gap|reseed_required,
subscription_readiness=not_ready)` с новой либо текущей
`reconnect_generation`. Далее запрещены новые `request_sent(place|cancel)`
на этой venue/environment, пока не будет append'нута successful REST
reconciliation:

```text
reconciliation(private_ws, gap|reseed_required) →
  [auth → ws_subscribe request/ACK для новой generation] →
  reconciliation(rest_reconcile, rest, matched, healthy, ready) →
  request_sent(place|cancel)
```

REST reconciliation описывает только категориальный результат и не содержит
account data, venue order identifiers, raw response либо raw frame. Если REST
reseed не завершился успехом, stream остаётся `reseed_required`; отправка
остаётся заблокированной. Это правило применимо к текущему
`run_id`/`venue`/`environment` независимо от `operation_id` отдельной ноги.

В `latency_summary.latency_intervals_ms` допускаются **ровно** следующие имена
интервалов (число milliseconds ≥ 0):

| Имя | Начало → конец | Ограничение |
|---|---|---|
| `local_prepare` | `order_prepared.event_monotonic_ns` → `request_sent.send_monotonic_ns` | Только локальная подготовка. |
| `request_ack_rtt` | `request_sent.send_monotonic_ns` → `ack_received.receive_monotonic_ns` | Round-trip/observed response time. |
| `local_response_processing` | `ack_received.receive_monotonic_ns` → append `ack_received.event_monotonic_ns` | Только локальная обработка после приёма. |
| `ack_terminal_receive` | `ack_received.receive_monotonic_ns` → `terminal_update.receive_monotonic_ns` | Время наблюдения от ACK до terminal update. |
| `exchange_to_client_observed` | evidence-adjusted `exchange_event_ts_utc` → `terminal_update.receive_monotonic_ns` | Разрешён только с `clock_offset_evidence`. |

`clock_offset_evidence`, если он присутствует, — object с ровно
`method` (`"ntp_offset"` \| `"venue_time_probe"`), `measured_at_utc` (RFC 3339
UTC) и `offset_ms` (number). Для `exchange_to_client_observed` он обязателен
как в `terminal_update`, так и в `latency_summary`; без него поле
`exchange_event_ts_utc` запрещено. Даже при evidence
`exchange_to_client_observed` — наблюдаемая offset-adjusted оценка, не
доказанная one-way path latency.

Ни `request_ack_rtt`, ни любой другой RTT нельзя представлять как one-way path
latency. Документ, dashboard и consumer обязаны называть это RTT/observed
latency, а не «latency до биржи».

## 6. Точная redaction denylist

Значения и ключи ниже запрещены **во всех** полях, включая вложенные objects,
arrays, `event_id`, opaque IDs и fingerprints. Сравнение имён ключей —
case-insensitive; запрещён также вариант с `-` вместо `_`.

```text
api_key
api_secret
secret
passphrase
password
authorization
cookie
set_cookie
signature
sign
access_token
refresh_token
bearer_token
private_key
client_secret
raw_payload
request_body
response_body
headers
canonical_request
operator_phrase
operator_message
operator_id
operator_user_id
operator_uuid
operator_handle
operator_name
operator_email
approver_id
approver_user_id
approver_uuid
approver_handle
approver_name
approver_email
approval_token
approval_phrase
approval_message
account_id
uid
member_id
wallet_address
exchange_order_id
order_id
client_order_id
clordid
balance
available_balance
equity
margin
position
account_value
price
qty
quantity
notional
fee
fill_price
fill_qty
```

`approval_token_fingerprint` — единственное явно разрешённое поле с
подстрокой `token`; оно должно удовлетворять точному определению HMAC из §4 и
не может содержать token, phrase или ID. Любое другое поле, ключ либо значение
с raw approval token/phrase/operator identity запрещено.

Запрещены также URL с query/credentials, полный endpoint/private URL, raw HTTP
request/response, raw WebSocket message, account values и любые значения,
которые можно использовать как секрет, account identity либо точные торговые
параметры. Разрешён только `symbol_alias`, category/bucket и
`request_fingerprint` по §4. Отсутствие запрещённых имён не оправдывает
сериализацию их содержимого под другим ключом.

## 7. Инварианты validator'а

1. Каждая непустая строка — полный валидный JSON object; версия, типы, enums и
   точный allowlist полей соответствуют этому документу.
2. Все общие correlation fields существуют, не `null`; `event_id` уникален;
   внутри `run_id` уникален `event_seq`, а `event_seq` и monotonic timestamp
   строго возрастают.
3. Каждый `auth.credential_presence` имеет ровно legacy all-boolean triplet
   либо current single-boolean форму из §4, без additional keys/values.
   Validator принимает обе historical-compatible формы, а новый writer
   создаёт только current-форму; existing lines не переписываются и не
   удаляются.
4. UTC имеет суффикс `Z`; `event_date` совпадает с UTC-датой timestamp и
   directory partition. Wall-clock и monotonic chronology выполняют §5.
5. Для одной ордерной ноги `operation_id`, `leg_id`, `dual_leg_id` и
   `request_fingerprint` остаются неизменными в связанных событиях.
   `dual_leg_id` имеет две различные `leg_id`; single-leg события не создают
   фиктивную вторую ногу.
6. Для каждого `operator_approval` `"granted"` соблюдаются HMAC-format
   fingerprint, уникальность `approval_record_id`/fingerprint и допустимый
   scope. Для каждого `"consumed"` существует один предшествующий неистёкший
   grant с теми же record/fingerprint/scope, `operation_id` равен
   `consumed_for_operation_id`, а второго consumption нет. Обязателен
   canonical order: durable `"consumed"` раньше `order_prepared` и
   `request_sent`; offline stream validation перед live `order_prepared`
   обязана принять эту цепочку. Sibling approval/consumption journals
   отклоняются.
7. `pre_send_gate` имеет только `gate_kind`/`gate_decision = "blocked"` и не
   имеет `order_prepared`/transport/order-state events с тем же
   `operation_id`. Для gate-blocked нельзя добавлять reconciliation: она
   допустима только при order lifecycle, в котором dispatch был начат.
   Единственное historical исключение — точная пара
   `legacy_pre_send_no_dispatch` из §4.3; validator принимает её только как
   read-only semantic supersession. Любая другая
   `reconciliation(order_state, inconclusive)` без `request_sent`
   отклоняется fail-closed.
8. Lifecycle соблюдает разрешённую последовательность. `terminal_update` не
   предшествует ACK; cancel не предшествует ACK; reject/abort имеют допустимый
   stage/reason и не скрывают terminal state. Для `post_only = true` без
   terminal до TTL обязателен `cancel_requested` с
   `"post_only_ttl_expired"` и последующий canonical recovery
   `reconciliation`; отсутствие ACK после dispatch описывается только
   `reconciliation(post_dispatch_ambiguity)`, не `reject`/`terminal_update`.
9. Private WS соблюдает §5.1: `ws_subscribe` начинается только после
   successful `auth`, имеет `subscription_readiness="not_ready"` при send и
   `"ready"` только в successful ACK. `ack_received` с `transport=ws_trade`
   для `place|cancel` никогда не terminal. Private WS `terminal_update`
   имеет `observation_source="private_ws"`, `sequence_state="healthy"` и
   предшествующий trade ACK той же ноги.
10. Для каждого `run_id`/`venue`/`environment` `reconnect_generation` не
    убывает; новая successful private WS login generation увеличивает его
    ровно на один. `sequence_state=gap|reseed_required` или
    `subscription_readiness=not_ready` блокирует последующий
    `request_sent(place|cancel)`, независимо от `operation_id`, до
    `reconciliation(private_stream_reseed, rest_reconcile, rest, matched,
    healthy, ready)`. Неуспешный или inconclusive REST reseed блок не снимает.
11. Writer acceptance gate требует единственного append writer и глобальный
   межпроцессный exclusive `/data/bbot/private/journal/.approval.lock`,
   охватывающий проверку grant, one-time consume, append и durable flush/sync.
   Допустимы только `.lock`, partition directories и `events.jsonl`; нельзя
   признать контракт соблюдённым при sidecar JSONL/state, включая
   `post_only_leases.jsonl`, нескольких writers или noncanonical consumption
   state.
12. `latency_summary` ссылается на реальные ранее append'нутые endpoints.
   Имена интервалов ровно из §5, значения неотрицательны; RTT не маркируется
   one-way. `exchange_to_client_observed` без полного offset evidence
   отклоняется.
13. `error_code` соблюдает allowlist и связь с `outcome`; raw exception и
   произвольные error texts отсутствуют.
14. Redaction denylist §6 не встречается ни в ключах, ни в значениях. Raw
   payloads, account values, реальные account/order IDs и торговые
   price/quantity/notional/fee, operator phrases/IDs и approval token не
   хранятся.
15. После restart каждый open `request_sent` без ACK/reject/terminal/final
   reconciliation получает canonical
   `reconciliation(post_dispatch_ambiguity, inconclusive)` до дальнейшего
   claim/retry; post-only recovery materializes only through canonical v1
   events.
16. Материализация private журнала происходит только под
   `/data/bbot/private/` (или явно разрешённым локальным fallback). Нет файлов
   private bot в `/data/live`, `/data/bars`, `/data/compacted`, `/data/spool`,
   `/data/bbot/journal/` или иных D roots.

## 8. Короткие redacted examples

Условные IDs, aliases и timestamps ниже не являются реальными credentials,
account/order IDs или результатами runtime.

```json
{"schema_version":"bbot.private.journal.v1","event_id":"evt_auth_example_001","event_type":"auth","event_date":"2026-08-19","event_ts_utc":"2026-08-19T12:00:00.100Z","event_monotonic_ns":1000000000,"run_id":"run_example_001","operation_id":"op_auth_example_001","event_seq":1,"venue":"bybit","environment":"testnet","outcome":"success","auth_method":"hmac","credential_presence":{"credentials_configured":true}}
```

```json
{"schema_version":"bbot.private.journal.v1","event_id":"evt_approval_grant_001","event_type":"operator_approval","event_date":"2026-08-19","event_ts_utc":"2026-08-19T12:00:30.000Z","event_monotonic_ns":1500000000,"run_id":"run_example_001","operation_id":"op_approval_example_001","event_seq":2,"venue":"okx","environment":"demo","outcome":"success","approval_action":"granted","approval_token_fingerprint":"a1b2c3d4e5f60718293a4b5c6d7e8f90123456789abcdef0123456789abcdef0","approval_scope":"live_order_send","approval_record_id":"approval_record_example_001","approval_expires_at_utc":"2026-08-19T12:05:30.000Z"}
{"schema_version":"bbot.private.journal.v1","event_id":"evt_approval_consume_001","event_type":"operator_approval","event_date":"2026-08-19","event_ts_utc":"2026-08-19T12:00:59.000Z","event_monotonic_ns":1900000000,"run_id":"run_example_001","operation_id":"op_leg_example_001","event_seq":3,"venue":"okx","environment":"demo","outcome":"observed","approval_action":"consumed","approval_token_fingerprint":"a1b2c3d4e5f60718293a4b5c6d7e8f90123456789abcdef0123456789abcdef0","approval_scope":"live_order_send","approval_record_id":"approval_record_example_001","approval_grant_event_id":"evt_approval_grant_001","consumed_for_operation_id":"op_leg_example_001"}
{"schema_version":"bbot.private.journal.v1","event_id":"evt_prepare_example_001","event_type":"order_prepared","event_date":"2026-08-19","event_ts_utc":"2026-08-19T12:01:00.000Z","event_monotonic_ns":2000000000,"run_id":"run_example_001","operation_id":"op_leg_example_001","event_seq":4,"venue":"okx","environment":"demo","outcome":"pending","dual_leg_id":"dual_example_001","leg_id":"leg_example_a","instrument_class":"linear_perpetual","symbol_alias":"BTC-USDT-SWAP","side":"buy","order_kind":"limit","quantity_bucket":"min_lot","notional_bucket":"under_100_usd","reduce_only":false,"post_only":true,"ttl_bucket":"short","request_fingerprint":"fp_example_001"}
{"schema_version":"bbot.private.journal.v1","event_id":"evt_sent_example_001","event_type":"request_sent","event_date":"2026-08-19","event_ts_utc":"2026-08-19T12:01:00.004Z","event_monotonic_ns":2004000000,"run_id":"run_example_001","operation_id":"op_leg_example_001","event_seq":5,"venue":"okx","environment":"demo","outcome":"pending","dual_leg_id":"dual_example_001","leg_id":"leg_example_a","request_kind":"place","request_fingerprint":"fp_example_001","transport_attempt":1,"send_monotonic_ns":2003500000}
{"schema_version":"bbot.private.journal.v1","event_id":"evt_ack_example_001","event_type":"ack_received","event_date":"2026-08-19","event_ts_utc":"2026-08-19T12:01:00.092Z","event_monotonic_ns":2092000000,"run_id":"run_example_001","operation_id":"op_leg_example_001","event_seq":6,"venue":"okx","environment":"demo","outcome":"success","dual_leg_id":"dual_example_001","leg_id":"leg_example_a","request_kind":"place","request_fingerprint":"fp_example_001","ack_state":"accepted","receive_monotonic_ns":2090000000}
```

Private WS flow ниже показывает только разрешённые категориальные признаки.
Он не содержит raw messages, account data или venue order identifiers:

```json
{"schema_version":"bbot.private.journal.v1","event_id":"evt_sub_send_001","event_type":"request_sent","event_date":"2026-08-19","event_ts_utc":"2026-08-19T12:02:00.000Z","event_monotonic_ns":3000000000,"run_id":"run_example_001","operation_id":"op_stream_001","event_seq":7,"venue":"okx","environment":"demo","outcome":"pending","request_kind":"ws_subscribe","transport_attempt":1,"send_monotonic_ns":2999000000,"transport":"ws_trade","reconnect_generation":0,"subscription_readiness":"not_ready"}
{"schema_version":"bbot.private.journal.v1","event_id":"evt_sub_ack_001","event_type":"ack_received","event_date":"2026-08-19","event_ts_utc":"2026-08-19T12:02:00.010Z","event_monotonic_ns":3010000000,"run_id":"run_example_001","operation_id":"op_stream_001","event_seq":8,"venue":"okx","environment":"demo","outcome":"success","request_kind":"ws_subscribe","ack_state":"received","receive_monotonic_ns":3009000000,"transport":"ws_trade","reconnect_generation":0,"subscription_readiness":"ready"}
{"schema_version":"bbot.private.journal.v1","event_id":"evt_gap_001","event_type":"reconciliation","event_date":"2026-08-19","event_ts_utc":"2026-08-19T12:02:10.000Z","event_monotonic_ns":4000000000,"run_id":"run_example_001","operation_id":"op_stream_001","event_seq":9,"venue":"okx","environment":"demo","outcome":"observed","reconciliation_scope":"private_stream_reseed","reconciliation_state":"inconclusive","observation_source":"private_ws","reconnect_generation":0,"sequence_state":"gap","subscription_readiness":"not_ready"}
{"schema_version":"bbot.private.journal.v1","event_id":"evt_reseed_001","event_type":"reconciliation","event_date":"2026-08-19","event_ts_utc":"2026-08-19T12:02:11.000Z","event_monotonic_ns":5000000000,"run_id":"run_example_001","operation_id":"op_stream_001","event_seq":10,"venue":"okx","environment":"demo","outcome":"success","reconciliation_scope":"private_stream_reseed","reconciliation_state":"matched","transport":"rest","observation_source":"rest_reconcile","reconnect_generation":0,"sequence_state":"healthy","subscription_readiness":"ready"}
```

## 9. Handoff и критерий готовности спецификации

Следующий implementation handoff — B Private Runtime для writer только в
`app/bot/private/**`; независимая проверка принадлежит B Private Validator.
Validation должен проверить JSONL, redaction, sequence и изоляцию D на
целевой VPS/mounted-storage среде. До этих отдельных работ этот документ —
спецификация, а не runtime implementation, proof of auth, account access,
order send либо live readiness.
