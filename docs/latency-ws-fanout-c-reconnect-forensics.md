# Форензика reconnect в standalone arm C

Трек `(D)`, блок: standalone WebSocket fan-out → receive loop → reconnect
accounting. Production collector не изменялся.

## Update: controlled B↔C `wsfanout_bc_20260812r1`

Связанные записи: [BC results](latency-ws-fanout-bc-results.md),
[BC live](latency-ws-fanout-bc-run-live.md),
[Cdiag results](latency-ws-fanout-cdiag-results.md).
Ниже — подтверждённая причина C в том же sequential run, где B прошла
reconnect-gate.

### Краткий вывод

В `C` подтверждены **97** настоящих listener-close: `6` OKX и `91` Bybit.
Все — `ConnectionClosedError`, `ws_close_code=1006`, classification `abrupt`.
Из 91 Bybit **22** несут явную причину библиотеки:

`sent 1011 (internal error) keepalive ping timeout; no close frame received`

Это client-side keepalive: probe отправил WebSocket ping и **не прочитал pong
за `ping_timeout=20 s`**. Остальные 69 Bybit и все 6 OKX — тот же 1006 без
текста 1011 (peer abort / гонка close). Независимый matched XRP ping той же
руки имел `0` connection_error: это не общий Bybit/host outage.

B в том же run, на том же хосте, с тем же WS-профилем (`batch 30/3 s`,
`retry 10 s`, omitted `max_queue`, `ping_interval/timeout 20/20`) имела
`0/0` reconnect. Единственный плановый фактор — full parse/calc non-XRP в C.

Cdiag (`wsfanout_cdiag_20260812r1`) использовал **тот же код и те же knobs**
и прошёл `0/0`. Дрейфа конфигурации Cdiag↔C нет; отличие — другое окно и,
по BC-C, попадание в backlog `max_queue`.

### Почему это stall чтения, а не stall loop

- Loop-lag C: `>200/>500/>1000 ms = 0/0/0`; max `100.113 ms` ровно в
  `18:11:08.629Z`, рядом с первыми двумя OKX close (`18:11:05.932/.939Z`).
  100 ms недостаточно для `ping_timeout=20 s`.
- CPU в ту же секунду `72.8%` одного ядра, затем вернулся к ~21%.
- Первые Bybit 1011 — `18:12:26Z` и далее, то есть спустя интервал keepalive
  после начала инцидента, не в момент 100 ms lag.
- `protocol_errors=0`; ping process `0` reconnect; FD `610/610`; RSS
  `~114 MiB`. Не cancel, не safety abort, не parse exception.
- Волны Bybit: `18:12–13` (6+24), `18:27–30` (2+20+6+7), `18:42–44`
  (12+3+10). Max wave `25/60 s`. 88 уникальных монет, почти все attempt `1`
  (только 3 монеты flapped до attempt `2`).
- B: `299` control_frames на биржу = subscribe ack non-XRP; JSON application
  ping за час не копился. Keepalive — именно WS opcode ping библиотеки.

Подтверждённый механизм: при default `max_queue` (websockets 16.1, обычно
`16`) inline `json.loads`+parse/calc в C не успевает снять кадры с горячих
Bybit сокетов. Библиотека **перестаёт читать TCP**, pong остаётся в kernel
buffer, keepalive объявляет `1011`, соединение падает как `1006`. B дренирует
raw быстрее и очередь не заполняет. Cdiag с тем же профилем просто не попал
в такой burst.

Не подтверждено как причина этого run: subscribe storm (первый drop на
`T+26 min`), Bybit IP rate-limit на connect, отсутствие JSON pong, host/NTP,
compactor (он стартовал только в последние ~16 s C), `max_queue=None` vs
default как изолированный фактор (Cdiag default = 0 reconnect).

### Следствие для патча

Минимальное исправление standalone probe: dedicated drain-task кладёт каждый
кадр в unbounded `asyncio.Queue`, parse/calc читает оттуда. Библиотека всегда
читает сокет (pong проходит), измерение не отбрасывается. B и C делят этот
путь. **Не меняются** batch `30/3 s`, retry `10 s`, omitted `max_queue`,
`ping_interval/timeout 20/20`, validity gates.

Повтор: `wsfanout_bc_20260813r2`.

---

## Историческая запись: `wsfanout_abc_20260812r2`

Связанный итог серии: [r2 results](latency-ws-fanout-three-arm-r2-results.md).
Запущенный диагностический repeat C с классифицируемыми close/reconnect
артефактами: [Cdiag live record](latency-ws-fanout-cdiag-run-live.md) и
[Cdiag results](latency-ws-fanout-cdiag-results.md). Cdiag прошёл свой
reconnect gate (`0` OKX, `0` Bybit), но одновременная смена batching, retry и
queue profile не даёт приписать improvement одному параметру.
Трек `(D)`, блок: standalone WebSocket fan-out → receive loop → reconnect
accounting. Это read-only разбор локально сохранённого полного experiment root;
production collector и VPS не изменялись.

## Краткий вывод

В `C` подтверждены **100 настоящих завершений listener-итерации** (не счётчик
обёртки): `6` OKX и `94` Bybit. Каждый прошёл одну и ту же цепочку:
`ConnectionClosedError(None, None, None)` → фиксированная пауза `3 s` →
`unplanned_close` → новая попытка/подписка. У исключения нет ни close code, ни
reason; поэтому доказать server rate-limit по этим артефактам нельзя.

Это не cancellation, safety abort, parse exception или незакрытый reconnect:
остановка была только в `01:16:22.823Z`, после всех событий, `protocol_errors`
не опубликованы, resource abort отсутствует, а финал показывает `600` active
connections до штатного shutdown и `700` subscription sends (`306` OKX,
`394` Bybit). Главный подтверждённый дефект измерения — harness не записывает
close code/reason и не различает peer TCP reset, server-side policy и путь
между ними. Поэтому «точная внешняя причина» не наблюдаема.

## Evidence из `arm_C/runtime.jsonl`

- Начало C: `00:16:22Z`; при `00:33:00.569Z` OKX/MANA получил
  `ConnectionClosedError(None, None, None)`, а reconnect был в
  `00:33:03.569Z`. Ещё два OKX — `00:33:25.400–25.401Z`; остальные —
  `00:39:41.890Z`, `00:43:30.116Z`, `00:46:50.142Z`.
- Bybit имеет коррелированные волны, а не ровный одиночный retry: `24` reconnect
  в минуту `00:39`, `34` в `00:40` (включая плотную последовательность
  `00:40:14.084–00:40:18.580Z`) и `29` в `01:10–01:13Z`.
  Примеры: `NEAR` `00:39:01.120Z` error → `00:39:04.121Z` reconnect;
  `SMCI` дошёл до attempt `3` в `01:10:38.677Z`.
- Все `100` reconnect имеют `attempt` `2` или `3`; unrecovered connections нет.
  Финальные счётчики: Bybit `connection_errors=94`,
  `unplanned_closes=94`, `unplanned_reconnects=94`; OKX соответственно `6`.
- `C` не завершалась по safety: `safety_abort` отсутствует, `stop_requested`
  только `01:16:22.823Z` с `duration_elapsed`; `clean_shutdown=true`.
  Loop-lag: `18 000` samples, `>200/>500/>1000 ms = 0/0/0`;
  p99 `1.310 ms`, max `32.541 ms`. CPU p95/max `24.95/38.86%`, FD max `610`,
  RSS max `114.28 MiB`.
- В `C` не было `protocol_errors` в final counters. Это исключает пойманные
  `json.JSONDecodeError`, `KeyError`, `TypeError`, `ValueError` в
  `full_handle`, но **не** доказывает отсутствие receive-buffer backlog:
  probe установил `max_queue=None`.
- Отдельный Bybit XRP ping зафиксировал один такой же
  `ConnectionClosedError(None, None, None)` в `00:39:01.455Z`, внутри первой
  волны C. Это подтверждает общий endpoint/path incident, но один ping не
  атрибутирует его бирже.
- `B` непосредственно перед `C` использовала те же `300` пар / `600` sockets
  и завершилась с `300/300` opens и subscription sends на каждой бирже без
  reconnect/error counters. Следовательно, постоянный лимит «300 standalone
  sockets на биржу» этим запуском не подтверждён.

## Фактическая topology и сравнение с collector

`C` создаёт **по одному socket на symbol на каждую биржу**: `300` OKX +
`300` Bybit = `600` initial connections и subscriptions, а не один
multiplexed socket с 300 topics. `expected_connections=600` и финальные
`connections_opened=306/394` это подтверждают. При активном production
collector на том же VPS (manifest: service active, PID `24505`) C была
co-resident; experiment root не содержит его per-exchange socket count.

| Свойство | Standalone C (`validation/ws_fanout_three_arm.py`, source at r2) | Collector D (`app/screaner_b_o.py`, repository source) |
|---|---|---|
| Topology Bybit/OKX | 1 topic, 1 socket на symbol: 300 на каждую биржу | То же: `bybit_listener`/`okx_listener` создаются для каждой пары; нет multiplexing |
| Старт | Один pair каждые `0.05 s`: одновременно OKX+Bybit, 20 new sockets/s на биржу | batches по 30 pairs, затем pause `3 s`: 30 tasks на биржу запускаются почти одновременно в batch |
| Подписка | Один `send()` сразу после connect; исходно 600 sends | Один `send()` сразу после connect; при ≈337 pairs исходно ≈337 sends на биржу |
| Ping/close | `ping_interval=20`, `ping_timeout=20`, `close_timeout=2` | Те же явные параметры |
| Receive/backpressure | `async for`; C делает `json.loads` и parse/calc каждого frame; **`max_queue=None`** | `async for`; full JSON/parse/calc каждого frame; `max_queue` не передан (library default, значение/version на VPS данным набором не подтверждены) |
| Compression/proxy | Не заданы явно; применяются defaults `websockets` 16.1, compression/proxy не логируются | Не заданы явно; фактическая версия/defaults VPS не сохранены в local evidence |
| После exception | Любой `Exception` логируется repr; fixed `3 s`, без jitter/backoff | Любой `Exception` логируется строкой; fixed `10 s`, без jitter/backoff |
| Close diagnostics | Только `repr(exc)`; code/reason не извлечены | Только текст exception; code/reason и per-leg counters также не структурированы |
| Cancellation | `stop_event`, затем cancel всех tasks после duration | signal cancellation закрывает текущий ws; обычный listener бесконечен |

Производственный факт ограничен имеющимся evidence: за 8-hour canary collector
оставался `active`, `NRestarts=0`, с ростом published rows; report отдельно
отмечает только `OKX candle5m ... no close frame` около `07:13/07:16Z`, не
book-listeners. Это **не доказывает**, что D не имел book reconnect: в D нет
per-exchange reconnect counters, а `/var/log/spread/runtime.log` данного окна
не был приложен к r2 artifacts.

## Ранжирование причин

1. **Подтверждено: abrupt remote/path WebSocket termination, причина не
   классифицирована.** `ConnectionClosedError(None, None, None)` у 100
   фактических listener failures, синхронные multi-symbol waves и совпадение
   одного independent Bybit ping. Нет wire trace/close frame, поэтому
   «Bybit rate limit» не является подтверждённым названием причины.
2. **Вероятно: exchange-side или network-path событие, преимущественно Bybit.**
   Волны 58 Bybit reconnect за две соседние минуты при нормальном CPU/loop-lag
   плохо согласуются с локальной одиночной parse exception. Но server, CDN,
   NAT/firewall и маршрут не разделены измерением.
3. **Возможный усилитель, не доказанная причина: co-resident socket/session
   policy и distinct C receive mode.** В C к работающему production добавлены
   300 Bybit sockets; B показывает, что это не простой постоянный ceiling.
   Отличие B→C — full per-frame work, однако низкий loop-lag, нулевые caught
   protocol errors и `max_queue=None` не позволяют ни обвинить, ни исключить
   per-socket backlog/flow-control.
4. **Не подтверждено: startup subscribe rate limit.** Начальная fan-out фаза
   завершилась примерно за 15 s, первый drop пришёл через ~16 min; delayed
   connection policy остаётся возможной, но прямого rate-limit ответа нет.

## Минимальное следующее испытание / design-level correction

Повторить только C как read-only shadow, сохранив topology, host, production
co-residency и universe, но сделать reconnect evidence классифицируемым:
сохранять `type(exc)`, `ws.close_code`, `ws.close_reason`, фазу
`connect/send/receive`, connection age, socket sequence и компактные
per-minute wave counts; зафиксировать фактические `websockets` defaults
(`max_queue`, compression, proxy). Добавить отдельный same-host Bybit ping
counter и production-book log/counter snapshot на то же окно. Это не патч D и
не меняет policy/лимиты до появления code/reason либо воспроизводимого
контролируемого различия.

## Граница вывода

Результат инвалидирует только сравнение `B↔C` в r2 для причинности
connection-versus-parse/calc. Он не отменяет тихие latency observations A/B,
но также не подтверждает production safety или отсутствие проблемы в D.
Ни production service, ни code path, storage, bars, compaction, backup,
retention, raw artifacts и Track B не менялись.
