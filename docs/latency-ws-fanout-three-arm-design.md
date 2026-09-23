# Трёхрукавный опыт: число WebSocket-соединений или обработка N потоков

> **Статус: `repeat running`** — `r1` завершена
> `2026-08-11T20:40:52Z` на NEW VPS `root@38.180.94.108`.
> Результат и ограничение измерения: [dashboard](latency-ws-fanout-three-arm-results.md).
> Valid repeat с raw pooled evidence: [r2 live record](latency-ws-fanout-three-arm-r2-run-live.md)
> и [r2 results placeholder](latency-ws-fanout-three-arm-r2-results.md).
> Будущие production-gates и граница Track (B): [контракт приёмки](latency-production-acceptance-contract.md).
> Исторический owner run — Track (D) Runtime Storage + Validation; дальнейшие
> действия только read-only, без production вмешательства. Реализация — отдельный shadow probe в
> `validation/ws_fanout_three_arm.py`; production collector не изменялся и не
> перезапускался. Единственное сознательное расхождение с production: probe
> не пишет records/parquet, а в `B` не декодирует non-XRP market-data payload.
> Fresh matched ping также standalone (`validation/ws_fanout_matched_ping.py`);
> он нужен, так как исторический `websocket-client` helper несовместим с
> установленным на VPS пакетом `websocket`.

## Вывод сверху

Этот опыт должен разделить две ещё неразделённые части `H1`: нагрузку от **N пар WebSocket-соединений** и нагрузку от **обработки сообщений N потоков** в одном Python-процессе. Предлагаются три строго изолированные shadow-руки `A/B/C` с одним XRP probe, одинаковым `N=300`, выключенными bars, без durable persistence и с fresh matched XRP ping.

Результат не устанавливает production-лимит и не открывает Track (B). Он может только дать доказательство, достаточное для последующего обсуждения политики пределов по числу соединений и/или обрабатываемых потоков. До выполнения требуется явное разрешение на новый shadow-only путь WebSocket ingest и parsing; production ingest, parsing, spread calculation и trading logic остаются frozen.

## 1. Блок конвейера и границы

- **Трек:** `(D) collection / storage reliability`, подзадача latency root-cause.
- **Проверяемый блок:** получение WebSocket-кадра → планирование `asyncio` loop → минимальная либо полная обработка book-сообщения. Persistence, bars и production unit исключены из нагрузки.
- **Production entrypoint:** `app/screaner_b_o.py` не меняется и не перезапускается.
- **Исполняемая среда:** один изолированный shadow-процесс на NEW VPS, не одновременно с другой рукой; runtime log — отдельный experiment root; первая материализация и durable destination для tick-данных отсутствуют.

## 2. Факты, гипотезы и выбор дозы

### Факты

1. Matched production XRP при `N≈337` имел тяжёлые хвосты: ранее p99 был порядка секунд, а в contemporaneous 26-минутном окне — `612/676` мс для OKX/Bybit и `25/10` dual-минут `>500/>1000` мс. Эта endpoint-точка **не является чистой shadow-точкой**: в production включены bars и отличается persistence.
2. Shadow `N=1`, `3`, `10`, `50`, а также `100` не показал тяжёлого хвоста. Для `N=100` p99 S/P был около `1.12–1.41×`, dual-минут `>500` и `>1000` — `0/26`.
3. `E2b` на `N=1` за 115 минут steady показал XRP shadow около matched ping: p99 S/P `1.09×` и `1.05×`, dual `>500/>1000` — `0/0`.
4. Поэтому наблюдаемая граница сейчас — примерно `100 < N ≤ 337`, но причинность `N→tail` не доказана.

### Проверяемая гипотеза

`H1a` — хвост возникает уже от количества одновременно открытых WebSocket-соединений / подписок, даже когда non-XRP сообщения не проходят обычный parse/calc путь.

`H1b` — хвост возникает главным образом от потока `json.loads`, извлечения quotes, расчёта и формирования records для N потоков на одном loop; сами соединения недостаточны.

Возможен смешанный исход: connection count создаёт часть scheduling/FD pressure, а обработка N сообщений добавляет основной хвост.

### Почему `N=300`

Фиксируется `N=300` пар, включая XRP, то есть по одной book WebSocket-связке на OKX и Bybit для каждой пары (`≈600` book connections). Это ближе к наблюдаемому production `N≈337`, чем `N=200`, и лежит существенно выше уже тихой точки `N=100`; значит, лучше способен воспроизвести режим, где должен проявиться эффект. Это всё ещё не утверждение, что `300` безопасно или что `337` является порогом. Перед каждым запуском обязательна проверка FD, CPU, памяти и network headroom; при её непрохождении опыт не запускается, а не понижается молча до другого `N`.

## 3. Контракт трёх рук

Все руки используют один и тот же зафиксированный на старте universe из 300 production-symbol pairs, обязательно с XRP, одинаковые версии кода/зависимостей, process limits и host. XRP — единственный измеряемый record path. Каждая рука имеет один fresh matched XRP ping того же класса, но отдельный от shadow-процесса.

| Рука | Соединения и подписки | Обработка XRP | Обработка non-XRP | Что изолирует |
|---|---|---|---|---|
| `A` baseline | Только одна XRP WebSocket-пара: OKX+Bybit | Existing shadow parse/calc и XRP record measurement | Нет | Сеть, протокол, XRP parse/calc и базовый process overhead |
| `B` connection-only fan-out | `N=300` пар: `≈600` book WS и подписки | Та же, что `A` | Кадры непрерывно дренируются и выбрасываются с минимально необходимой обработкой | Дополнительный эффект числа connections/subscriptions/FD/network ingress без обычного parse/calc/record non-XRP |
| `C` full fan-out | Тот же `N=300`, тот же universe и connection topology, что `B` | Та же, что `A` | Current shadow parse/calc handling для всех N потоков; non-XRP records только в in-memory counters и отбрасываются | Добавочный эффект полного message-processing поверх результата `B` |

`A` намеренно имеет одну пару connections, тогда как `B/C` имеют `N`: она оценивает floor. Чистое различение `B` и `C` является главным тестом H1a против H1b, поскольку у них одинаковы universe и connection count.

### Точная минимальная обработка в `B`

Для каждого non-XRP data frame разрешены только:

1. принять и снять кадр из receive buffer, чтобы не создать искусственную TCP/WebSocket backpressure;
2. дать библиотеке выполнить обязательную обработку WebSocket control frames (`ping`/`pong`/`close`) и transport-level reconnect;
3. обработать только те exchange-level control messages, без которых нельзя подтвердить subscription, ответить на обязательный application ping или классифицировать явную ошибку/reconnect;
4. увеличить счётчики `frames_received`, `bytes_received`, `discarded_data_frames`, `control_frames`, `protocol_errors` и сразу освободить payload.

Для обычных non-XRP market-data frames `json.loads` **не является неизбежным**, если соединение подписано ровно на один заранее известный symbol/topic и библиотека возвращает raw text/bytes: frame может быть отброшен без JSON decode. Если конкретная биржа смешивает control и data frames либо требует JSON application-pong, допускается только `json.loads` внешнего control envelope для такого кадра; нельзя извлекать bids/asks, нормализовать числа, строить quotes, вызывать calc, создавать event/record или писать в buffer. Доля decoded control frames и число `json.loads` должны быть отдельно залогированы. Если client library безусловно делает JSON decode до callback, это фиксируется как residual confounder: `B` тогда изолирует отсутствие domain parse/calc/record, но не JSON-decode cost.

### Persistence policy

Во всех трёх руках `collect_bars=false`; durable parquet, spool, publisher и mounted storage не запускаются. Допускаются только XRP latency samples и агрегированные per-minute/per-process counters в памяти, периодически сериализуемые в отдельный structured runtime log. Если runtime log является локальным файловым I/O, его формат, log level, flush policy и sampling должны быть одинаковы во всех руках. Никаких parquet field/schema changes в первом запуске.

## 4. Сопоставимость и то, что нельзя удержать постоянным

1. Руки идут **строго по одной**. Предпочтительный порядок — случайная перестановка `A/B/C`, выбранная до старта; после первого блока выполнить вторую перестановку в другой time-class, если ресурсное окно позволяет. Это противодействует market-time effects лучше, чем фиксированный порядок `A→B→C`.
2. Каждая рука: 60 минут wall-clock, первые 10 минут warmup исключаются, целевой steady-интервал — 50 минут. Минимально допустимы 45 минут и 5 минут warmup, если окно обслуживания ограничено, но это слабее для редких dual events.
3. XRP, список 300 pairs, exchange endpoints, subscribe batching, Python environment, CPU affinity/cgroup limits, `ulimit -n`, log configuration и ping configuration фиксируются перед серией. Universe сохраняется как manifest с порядком symbols и hash.
4. У `B/C` одинаковы N, universe, число socket connections и subscriptions. У `A` намеренно иное N, поэтому он не может один отделить FD pressure от processing; это делает сравнение `B↔C` обязательным.
5. Нельзя полностью зафиксировать exchange message rates, биржевые очереди, route latency, market volatility, server-side rate limiting/connection treatment, reconnects и внешний host load. Ping нормализует лишь общий XRP network floor, не объём non-XRP traffic. Их надо наблюдать, а не выдавать за контролируемые константы.

## 5. Инструментация и метрики

### Основные latency-метрики

- XRP trigger-leg `delivery_latency_ms = local_recv_ts_ms − exchange_ts`; семантика существующей метрики не меняется.
- Matched XRP ping: OKX `latency_ms`, Bybit `age_ts_ms`.
- `S/P p99` по каждой ноге.
- Для S и P: `n`, `p50`, `p95`, `p99`, `max`; доля отрицательных latency.
- Число и доля минут, где обе trigger-ноги превышают `500` мс и `1000` мс: `dual>500`, `dual>1000`.

### Shadow-only наблюдаемость нагрузки

- Новый opt-in event-loop lag probe: монотонный callback с периодом `100–200` мс, `lag_ms = now_monotonic − expected`; per-minute `p50/p95/p99/max` и counters `>200/>500/>1000` мс. Он не меняет timestamp в ingest callback.
- Per-arm и per-minute: `frames/s`, `bytes/s`, XRP/non-XRP frame rate, decoded-control `json.loads/s`, reconnects, protocol errors, dropped/discarded data frames, internal receive exceptions.
- Процесс: CPU % и CPU time, RSS, open FD count и FD limit, context switches если доступно read-only; process/thread count; host loadavg и network RX bytes.
- Quality/accounting: expected vs active connections, subscription acknowledgements, explicit dropped-frame counters, ping coverage, warmup/steady boundaries, clean shutdown result.

Метрики собираются в runtime logs, не в parquet. Для анализа сохраняются отдельные summaries, но raw market data и durable storage не являются результатом опыта.

### Acceptance criteria качества измерения

Рука считается `valid` только если:

1. expected universe и XRP подтверждены; у `B/C` active book connections/subscriptions соответствуют `N` на обеих биржах с заранее определённой допустимой краткой reconnect-deviation;
2. полный matched ping overlap есть не менее 45 минут после warmup;
3. XRP trigger samples обеих ног достаточно полны для всех steady минут, а ping не пуст и не имеет систематического outage;
4. dropped-frame, reconnect и protocol-error counters опубликованы, а не отсутствуют; непредвиденный drop/reconnect-rate выше predeclared budget помечает руку `degraded`;
5. процесс завершается штатно, без записи в production root, без вмешательства в production unit;
6. resource headroom не был исчерпан. FD exhaustion, OOM, cgroup throttle либо uncontrolled restart — не доказательство H1, а `measurement_failure` или отдельный capacity incident.

## 6. Правила статистического вердикта

Основной анализ — per-arm steady-интервал и matching с его собственным ping. Предварительные thresholds намеренно ориентированы на наблюдение: до `N=100` S/P p99 был около `1×` (максимум в evidence `1.41×`) и не было dual tail; production `N≈337` имел многократный tail. Они являются decision thresholds, не оценкой причинности.

| Наблюдаемый паттерн | Классификация H1 | Допустимый вывод |
|---|---|---|
| `A` тихая; `B` имеет воспроизводимый heavy tail: обе ноги `S/P p99 ≥5×` либо `p99≥500` мс при `P p99≤100` мс, и есть `dual>500`; `C` не существенно хуже `B` | Преимущественно connection-count / FD / subscription fan-out | Нужна отдельная проверка FD/OS/server-rate-limit механизма; processing N не требуется для появления эффекта |
| `A` и `B` около ping (`S/P p99≤2×`, `dual>500≈0`); `C` соответствует heavy-tail signature выше | Преимущественно process-message load | `json.loads`/domain parse/calc/record path N — необходимый фактор в данном shadow runtime |
| `B` хуже `A`, а `C` устойчиво хуже `B`; оба отличаются от тихого ping | Смешанная причина | Connections и processing оба вносят вклад; следующий опыт должен измерять их веса, а не выбирать один лимит |
| Все руки тихие, включая `C`, при valid coverage | Не воспроизведено / H1 не подтверждена в этом окне | Не считать H1 опровергнутой: production confounders (bars, persistence, coexistence) остаются; вернуть анализ к ним |
| Ping шумный, coverage неполное, N/subscriptions не достигнуты, drops неучтены, FD/OOM/restarts, либо lag probe расходится с логами | Measurement failure | Повторить только после устранения измерительного дефекта; никакой политики N из этого результата |

Для снижения влияния времени сильный verdict требует одного и того же паттерна хотя бы в двух randomized runs, либо явно маркируется как `provisional`. Если p99 промежуточен (`2–5×`) без dual `>500`, результат — `partial / inconclusive`, а не выбор между H1a и H1b. Max без устойчивого p99/dual pattern не является достаточным доказательством.

## 7. Матрица решения

| `A` | `B` | `C` | Классификация | Следующее действие |
|---|---|---|---|---|
| около ping | heavy tail | не хуже `B` | Connection-count dominant | Проверить FD/OS/rate-limit и повторить connection dose; не менять production |
| около ping | около ping | heavy tail | Process-message dominant | Разделить JSON decode, calc и record path в следующем shadow дизайне |
| около ping | хуже ping | ещё хуже `B` | Mixed | Количественный dose по connections и processed streams, затем design review |
| около ping | около ping | около ping | Не воспроизведено | Проверить bars/persistence/coexistence; H1 не закрыта |
| невалидна любая критичная рука | — | — | Measurement failure | Исправить experiment harness и повторить без production changes |

## 8. Runtime safety и lifecycle

1. Только один shadow process на руку; production `spread-collector` не останавливается, не рестартует и не меняет конфигурацию.
2. У каждой руки свой experiment root и runtime-log path, например `/data/experiments/ws_fanout_<run_id>/`; production `/data/live`, spool, logs и mounted storage не являются ни первой материализацией, ни durable destination.
3. До старта: read-only headroom snapshot CPU/RSS/load, FD limit/usage, disk free для logs, active production PID, expected `≈600` additional sockets, cgroup/system limits. Predeclared safety budget и операция stop на shadow должны быть утверждены до запуска.
4. На root и в runtime log ставятся ownership markers `DO_NOT_TOUCH` с `run_id`, PID, owner, start/end, allowed paths и запретом на touching production. Reset — только штатный stop shadow process, проверка закрытия сокетов и архивирование его logs; не delete backlog и не truncate logs.
5. Остановка по resource safety — controlled stop только экспериментального PID с фиксацией `safety_abort`; production остаётся нетронутым. Такой abort не интерпретируется как latency result.

## 9. Минимальная поверхность реализации и владение

Первый запуск предпочтительно требует **нового standalone shadow probe module**, а не env-gated ветки в `app/screaner_b_o.py`: это лучше сохраняет frozen production runtime и явно отделяет `B` discard semantics от существующего parse path. Допустимый минимум:

- новый standalone module и launcher/config только для `A/B/C`;
- отдельный matched XRP ping invocation;
- structured runtime logging, in-memory counters и shadow-only loop-lag probe;
- отдельный validation/analyser script, читающий logs без изменения parquet schema.

Нельзя реализовывать `B` как «тихий» branch в production listener без отдельного согласования: это затрагивает frozen WebSocket ingest и parsing semantics даже при default-off flag.

| Зона | Владелец до выполнения |
|---|---|
| Контракт логов, metric names и решение не писать parquet | Schema Contract Agent |
| Standalone WS probe, `A/B/C`, launcher и isolation | Runtime Storage Agent после frozen-area unlock |
| Runbook, headroom gate, accounting и verdict validation | Validation Agent |
| Независимая атака на design/patch и силу вывода | Review Critic Agent |
| Русская стилистика после фиксации смысла | Text Stylist Agent |

**Явно требуемое разрешение пользователя:** разрешить создание и запуск standalone shadow-only WebSocket probe, который использует existing subscription/protocol configuration, но добавляет controlled discard path для non-XRP в `B` и shadow-only event-loop probe. Это не является разрешением менять `app/screaner_b_o.py`, production WS ingest/parsing/spread, production persistence или Track (B).

Если позднее XRP latency или новые metrics нужно писать в parquet, сначала обязателен отдельный schema review: fields, versioning, reader compatibility, first materialization и durable destination. Рекомендация для initial run — runtime logs, чтобы не открывать schema/storage handoff.

## 10. Handoff к будущей политике N

Только после двух valid randomized repetitions с согласованным паттерном можно вынести на будущий Track (B) предложение limit policy:

- если `B` сам воспроизводит tail, политика должна рассматривать лимит **WebSocket connections/subscriptions per process** и подтверждённый FD/resource budget;
- если только `C` воспроизводит tail, политика должна рассматривать лимит **processed streams/messages per process**, а не делать вывод о number of connections;
- если исход mixed, нужны оба ограничителя и evidence о худшем из них;
- если не воспроизведено, лимит не предлагается.

Даже сильный результат является входом в спецификацию и review будущей policy; он не открывает Track (B), не меняет production limit и не разрешает production patch автоматически.

## 11. Рекомендуемый следующий шаг

Получить один явный frozen-area unlock на standalone shadow probe и утвердить preflight safety budget. Затем Schema Contract Agent фиксирует log contract без parquet, Runtime Storage Agent готовит только isolated harness, Review Critic проверяет, что `B` действительно не выполняет domain parse/calc/record, а Validation Agent утверждает runbook до первого запуска.
