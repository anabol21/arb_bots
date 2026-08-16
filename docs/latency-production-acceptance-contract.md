# Контракт приёмки задержек для будущего production

> **Вердикт:** это контракт будущей production-приёмки, а не разрешение
> включить Track (B), не текущий лимит `N` и не доказательство, что какая-либо
> нынешняя конфигурация уже ему соответствует. r1 A/B/C имеет
> `inconclusive / measurement-limited` verdict. r2 был запланирован с raw
> samples, но его artifacts пока недоступны независимой проверке; его не
> следует называть valid repeat до проверки. Контекст:
> [результаты A/B/C r1](latency-ws-fanout-three-arm-results.md),
> [r2 result status](latency-ws-fanout-three-arm-r2-results.md),
> [pre-production gaps](latency-pre-production-gap-dashboard.md),
> [дозовый опыт](latency-dose-n-results.md) и `gate #1` в
> [дорожной карте](program-roadmap.md#3-gate-перед-треком-b).

## 1. Объект приёмки

**Профиль конфигурации** — неизменяемая единица валидации. Его manifest обязан
зафиксировать:

- класс хоста;
- `N` обрабатываемых пар и `N` WebSocket-подписок;
- включены ли bars;
- режим и периодичность persistence;
- уровень логирования;
- все co-resident процессы;
- лимиты CPU, памяти и файловых дескрипторов.

Изменение любого поля создаёт новый профиль и требует отдельной приёмки. Профиль
проходит контракт только после фиксированного warmup не менее 10 минут и **двух
независимых окон по 60 минут**; окна не должны быть непрерывными частями одного
запуска.

## 2. Предлагаемые gates профиля

Числа ниже — консервативные **предлагаемые требования**, которые надо подтвердить
на накопленных данных. Они не выведены как исторически доказанный безопасный
порог.

| Область | Gate в каждом 60-минутном steady-окне | Условие измерения |
|---|---|---|
| Delivery latency | pooled p99 `<100 ms` отдельно для trigger-leg OKX и Bybit | raw samples либо mergeable per-session histogram; нельзя считать percentile от минутных percentiles |
| Нормализация по сети | matched XRP ping есть весь steady-интервал; `S/P p99 ≤2.0×` на каждой ноге | `2.0×` — предложенный guardrail с запасом над тихими low-`N` наблюдениями, не историческое доказательство |
| Общие спайки | `dual>500 ms ≤1` минута из 60; `dual>1000 ms =0` | минута засчитывается, когда обе trigger-leg превысили порог; это предлагаемые budgets |
| Соединения и ошибки | `0` неучтённых drop; `0` protocol/internal receive error; не более `2` reconnect на trigger-соединение и `0.1%` aggregate reconnect events от connection-minutes за окно | reconnect, drop и error считаются отдельно для каждой биржи и публикуются даже при нуле. Кластер planned disconnect (`ws_wave_*_60s`) не является gate и не блокирует Track B (2026-08-16) |
| Буфер / publish | `0` событий переполнения или backpressure; вход, буфер, publish и reject имеют сверяемое accounting | ненулевой рабочий buffer сам по себе не считается нарушением |
| Event loop | pooled p99 loop lag `<20 ms`, max `<250 ms` | raw samples либо mergeable histogram |
| Ресурсный запас | CPU p95 `<75%` назначенной квоты; RSS p99 `<70%` memory limit; FD max `<70%` FD limit | snapshot не реже одной секунды; превышение — непрохождение, не причинный диагноз |

`S` — screener delivery latency, `P` — matched XRP ping той же биржи. Любое
нарушение или неполный набор артефактов означает `not accepted`, а не
«предварительно принято».

Поправка 2026-08-16: порог `wave_60s ≤8` снят с требований Track B и с
pre-B canary. Пачка именованных planned close (типично Bybit 1006) считается
артефактом сети/биржи, если unplanned=0, unrecovered=0 и тики fail-closed.
Счётчик `ws_wave_*` остаётся в логе. Исторические лимиты wave в standalone
fan-out probe (`≤3/60 s`) не переписываются: это validity тех прогонов, не
вход в B.

## 3. Требуемые доказательства

Для каждого окна обязательны:

1. structured runtime logs с timestamp, trigger-leg latency, reconnect/drop/error,
   buffer/publish/backpressure, loop lag, CPU/RSS/FD и границами warmup/steady;
2. raw latency samples или mergeable histogram, из которого можно получить
   pooled per-session p99;
3. неизменяемый run-config manifest с полным профилем конфигурации;
4. exact production-like background profile: список co-resident процессов,
   их версии, лимиты, состояние и время запуска;
5. matched XRP ping обеих бирж с подтверждённым overlap;
6. отчёт валидации с числителем, знаменателем, потерями samples и verdict по
   каждому gate.

На первом этапе изменения parquet schema не нужны: эти evidence-артефакты могут
жить в runtime structured logs и отдельных файлах эксперимента. Если их позднее
нужно записывать в parquet, сначала требуется отдельный schema review.

## 4. Каузальная матрица

| Категория | Единственный различающий controlled experiment | Что поддержит | Что ослабит |
|---|---|---|---|
| buffer/publish path | Одинаковый профиль и поток сообщений, только publish/buffer путь `off/on`; accounting и raw histogram включены | tail/loop lag появляется при `on` при стабильных WS и нагрузке | оба состояния проходят при сопоставимых ресурсах и coverage |
| WS connection/FD/event-loop fan-out | Валидный повтор A/B/C: одинаковые `N` и universe у `B/C`, raw/mergeable histogram и заранее заданный reconnect budget | `B` хуже `A`, а `C` не хуже `B` | `A≈B`, но `C` хуже `B`; либо все руки тихие в двух valid runs |
| per-message parse/calc workload | Тот же `B↔C` валидного A/B/C, где различается только обработка non-XRP сообщений | `A≈B`, `C` хуже `B` при допустимых reconnect/drop | `B≈C` при valid coverage и ресурсном запасе |
| concurrent non-collector process / host contention | Парный `background off/on` shadow опыт на одном хосте и одном профиле, в pre-approved safe load; порядок рандомизирован, background profile фиксирован | tail/loop lag воспроизводимо растёт только при `on` | нет устойчивой разницы при одинаковом `N`, сети и ресурсах |

Один опыт может ослабить гипотезу, но не доказывает единственную причину, если
одновременно не прошли quality gates.

## 5. Текущий статус evidence

| Категория | Статус | Обоснование |
|---|---|---|
| buffer/publish path | `indeterminate` | E0 видит `buffer_size>0` во всём окне и не видит `queue_depth>0`/`backpressure_hit`; это не подтверждает и не исключает path как причину ([E0](latency-e0-evidence-20260805.md)) |
| WS connection/FD/event-loop fan-out | `indeterminate` | r1 не сохранил pooled screener p99, а `C` имела 229 reconnect/error events. r2 raw repeat пока не проверен, поэтому не обновляет этот статус ([r1](latency-ws-fanout-three-arm-results.md), [r2](latency-ws-fanout-three-arm-r2-results.md)) |
| per-message parse/calc workload | `indeterminate` | `C` r1 не показала tail, но measurement не удовлетворяет собственной строгой матрице; r2 raw repeat пока не проверен ([r1](latency-ws-fanout-three-arm-results.md), [r2](latency-ws-fanout-three-arm-r2-results.md)) |
| concurrent non-collector process / host contention | `not tested` | Контролируемый фактор `background off/on` не запускался; сравнение OLD/NEW не удерживало фон постоянным ([host N=100](latency-host-n100-results-20260811.md)) |
| Низкие `N` без тяжёлого хвоста | `supported` | Shadow `N≤100` был около matched ping, а production `N≈337` имел tail, но production-конфаундеры не разделены ([dose N](latency-dose-n-results.md), [host N=100](latency-host-n100-results-20260811.md)) |

Малый `N` **не исключает** background contention. Взаимодействие нагрузки может
быть нелинейным: тот же фоновый процесс может проявляться только после роста
WebSocket, CPU, FD или очередей. Кроме того, comparison не обращал и не
удерживал фоновую нагрузку: на OLD compactor был остановлен, а NEW одновременно
делил хост с production `N≈337`. Направления этих конфаундеров противоположны,
поэтому спокойный low-`N` не является тестом гипотезы о соседних процессах.
Compaction и backup остаются кандидатами/конфаундерами, но не подтверждённой
причиной.

## 6. Governance и граница Track (B)

Контракт может стать входом в будущую спецификацию лимита `N` для Track (B)
только после его валидации для конкретного профиля. Он **не** разрешает
реализацию Track (B), production-конфигурацию или изменение frozen ingest,
parsing, spread calculation и trading logic.

Перед любым будущим применением результата обязательны:

- `Runtime Storage` и `Schema Contract` — только если меняются runtime evidence,
  persistence или parquet contract;
- `Validation` — независимая проверка артефактов, двух окон и quality gates;
- `Review Critic` — независимая проверка причинного вывода, конфаундеров и
  границы будущей policy.

## 7. Один следующий шаг

Сначала без изменения VPS artifacts получить и проверить полную локальную
копию [r2](latency-ws-fanout-three-arm-r2-run-live.md): manifests,
`runtime.jsonl`, ping, обе raw delivery CSV и raw loop-lag CSV для A/B/C.
Только после строгого validity verdict можно применить A/B/C matrix. **Только
если A/B/C валиден**, запускать отдельный парный опыт с фоновым процессом
`off/on`; иначе background-фактор смешается с неразрешённой неопределённостью
первого опыта. Полный ranked plan зафиксирован в
[pre-production gap dashboard](latency-pre-production-gap-dashboard.md).
