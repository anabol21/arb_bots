# План эксперимента: полный цикл `synthetic_roll` с проверкой ответов бирж

Это план, не выполненный эксперимент. Сейчас нет live-заявок, сетевых вызовов, runtime-правок или выкладки. Patch обработчика находится в изолированной локальной копии `/private/tmp/arb_bots-exchange-response-20261004`, ветка `codex/exchange-response-handler`, поверх `84baab57c7a85ff0f0276fea2939c5764b6bf182`.

## 1. Блок конвейера

Проверяется весь штатный путь: синтетический сигнал → существующий chooser выбирает монету из допустимого пула → обычные instrument/size/risk gates и штатный qty mapper → формирование и отправка заявок → trade ACK и private order updates → проверка текущих IDs, терминального статуса, полного cumulative fill qty и положительной конечной avg price обеих ног → запись `open` → возврат в watcher → через 10 секунд синтетический сигнал закрытия через тот же manager и reduce-only путь → запись `closed` → read-only подтверждение flat/no open orders → следующий случайный выбор.

Обработчик ответа проверяется начиная с входящих trade ACK/private updates после `ws.send`; pre-send отказы и invalid-order live cases вне scope. ACK сам по себе не является подтверждением исполнения. Для Bybit сверяются `orderStatus`, `cumExecQty`, `avgPrice`; для OKX — `state`, `accFillSz`, `avgPx`. Execution time биржи, socket-arrival time и время передачи/обработки приложением сохраняются как разные величины.

## 2. Файлы и модули

- `app/bot/private/place_send.py` — ожидание и классификация ответов, сверка IDs, заполнения и цены.
- `app/bot/private/send_legs.py`, `ws_trivial_dual_leg.py`, `ws_messages.py`, `order_sign.py` — штатное построение и отправка двух ног.
- `app/bot/private/ws_warm_loop.py`, `ws_warm_session.py`, `wire_transcript.py` — существующий websocket owner pump и wire capture.
- `app/bot/runtime.py`, `app/bot/synthetic_policy.py`, `app/bot/theta_trade_manager.py` — вход сигнала, chooser, manager/watcher, open/close и возврат к следующему циклу.
- `docs/synthetic-roll-response-handling-2026-10-04.md`, `architecture.md` — контракт ответа и схема контура.
- `validation/b0-promotion-2026-10-03/B0_VV_LIVE_RESULT_2026-10-04.md`, `validation/hotpath-six-live-2026-10-04.md` — прежние отчёты. Они содержат сводки реальных ACK/fill, но raw private frames не сохранены в репозитории как проверенные replay fixtures.

## 3. Варианты проверки

| Подход | Что проверяет | Ограничение |
|---|---|---|
| Только локальные synthetic fixtures | Локальные ветви обработчика для заданных JSON-сообщений | Не доказывает, что реально пришли те же сообщения и порядок |
| Только live open/close | Настоящие ACK, private fills, журнал и переход watcher | Сам по себе не воспроизводит timeout, дубликаты и перестановки |
| Offline stale-`reqId` gate → capture → до трёх штатных live циклов → replay записанных кадров | Полный manager lifecycle плюс контролируемый replay сообщений, полученных в live цикле | Требуются capture в существующем reader и ограниченный experiment input для сигнала/задержки |

Рекомендуется третий подход. Capture ставится в единственном существующем owner pump: второй `recv`/reader не добавлять. Сохранять `run_id`, venue/socket generation, порядковый номер, intent/phase и текущие request/client/order IDs; для каждого кадра фиксировать socket-arrival wall+monotonic, время передачи обработчику, а биржевое execution timestamp — отдельным полем. Не писать auth-заголовки, ключи или секреты. Потеря кадров или ошибка записи делает capture этого цикла неполным.

## 4. Риски и отказные состояния

- До старта нужен read-only preflight: свежие account positions/open orders и instrument metadata обоих venue. Предыдущий B0 отчёт оставил Bybit `+5 XRP`, OKX `−0.05` контракта и `ready=false`; пока аккаунты не подтверждены flat и без открытых заявок, live-цикл не начинать. Старые позиции автоматически не закрывать.
- Timeout, partial, asymmetric fill, чужие/несовпавшие IDs, несходящаяся qty/цена или ошибка/неподтверждённый journal прекращают всю кампанию. Не отправлять повтор, recovery или автоматический close; сохранить наблюдаемое состояние и остановить новые сигналы для этой кампании.
- Обычное закрытие разрешено только после двух полных terminal fills и подтверждённой записи `open`. Закрывать фактически открытую/заполненную quantity каждой ноги через reduce-only, не пересчитывать close qty из нового nominal и не пропускать закрытие из-за того, что цена изменила долларовый эквивалент выше $15. Штатное соответствие close qty реальному open fill нужно проверить до live; если оно не выполняется, это блокирующий gap. После close следующий выбор разрешён только после `closed` и read-only flat/no-open-orders проверки.
- Ошибка до `ws.send` не является exchange reject ACK. Ранее проверенные reject ACK не повторять реальными заявками. Raw reject payload в артефактах не подтверждён.
- Входящие private order updates — критерий fill; REST-снимок не подменяет доказательство получения websocket frame. Если реального partial/error payload нет, соответствующий replay case помечается synthetic, а не live-валидированным.
- Не заявлять, что реальные partial были получены, пока capture не покажет их raw frames. Предыдущие отчёты со сводками не заменяют кадры.

## 5. Минимальный эксперимент

### Подготовка и ограничители

1. Сначала выполнить узкий offline stale-reject correlation gate: старый reject ACK только с `reqId` не должен завершать ожидание новой заявки. Это discovery gate, не genuine fixture. Если он падает, live-часть не начинать; исправление/повтор gate — отдельная offline работа.
2. Один раз пройти конечный candidate pool, определённый пересечением текущего profile coin pool и реально поддерживаемых Bybit+OKX instrument routes. Для каждого кандидата проверить свежие product/type, `lotSize`/минимумы/`ctVal` и результат штатного mapper на текущих ценах. Для **открытия каждой ноги** nominal должен быть **$7–15**; предпочесть минимальный размер, который штатный mapper допускает в этом диапазоне (ориентир — около $7). Оставить chooser и qty mapping как есть; не подменять размер, не повышать nominal и не делать бесконечный redraw при неудаче. Кандидата, который не проходит preflight, записать как skip без заявки. Если eligible pool пуст — остановиться.
3. Не требовать уникальности монет: chooser выполняет обычный `rng.choice(eligible_pool)`, поэтому повторы допустимы. Сохранить seed и состав пула в experiment metadata; не менять алгоритм выбора ради отсутствия повторов.
4. Сгенерировать синтетический open сигнал через существующую точку входа decision/manager. Затем оставить тот же manager в watcher после успешной записи `open`. Для close нужен сигнал через тот же manager через 10 секунд после обеих terminal fills и подтверждения `open` journal. Если штатная конфигурация не поддерживает timed hold, нужен узкий experiment input/harness gate, который подаёт close decision на эту точку через 10 секунд; он не вызывает sender напрямую и не обходит manager/risk/size gates. В существующей политике timed hold не подтверждён.

### Ограниченная последовательность

Провести до **трёх** последовательных циклов в одном ограниченном manager/watcher run с новым journal root и уникальными IDs для каждого цикла/ноги/фазы. Каждый цикл: случайный eligible coin → обычный dual open → дождаться полных исполнений и `open` → возврат в watcher → 10 секунд → synthetic dual reduce-only close через manager на фактически открытую/filled quantity каждой ноги → `closed` → свежая GET-проверка flat/no open orders. Close qty не пересчитывается от opening nominal или текущей цены, поэтому движение цены выше $15 не должно задерживать закрытие. Проверить это свойство штатного close path до live; если не подтверждено, сначала закрыть этот gap. Только после flat-проверки разрешать следующий выбор. Повторы coin допустимы. Любая остановка заканчивает весь run.

На один успешный цикл приходится 2 paired intents и 4 заявки: 2 open + 2 close. Максимум кампании — **6 paired intents / 12 order requests** (3 цикла × 2 intents × 2 venue); ACK, частные обновления и offline replay заявками не считаются. Preflight skip заявок не создаёт, но не разрешает подменить qty. Если после предыдущего закрытия следующая свежая проверка показывает позицию/open order, дальнейший выбор остановить.

| Фаза каждого цикла | Проверка и запись | Максимум заявок |
|---|---|---:|
| Synthetic open → manager → send | Пассивно записать signal, gate/size outputs, client/order IDs, `ws.send`, ACK и private updates. На обеих ногах требовать текущие IDs, terminal Filled, exact cumulative qty и конечную положительную avg price; затем проверить `open` journal. | 2 |
| Watcher → 10s hold → synthetic close | Зафиксировать возврат watcher и момент confirmed `open`; после 10 секунд передать close decision через ту же manager-точку, reduce-only обеих ног на фактически filled quantity, затем проверить полные close fills и `closed` journal. Не ограничивать close новым nominal $15. | 2 |
| Flat verification | Read-only positions/open orders; продолжать только при подтверждённом flat состоянии. | 0 |

После первого успешного цикла взять его настоящие ACK/private frames и выполнить offline replay до продолжения следующих циклов: genuine frame success, reordered delivery, duplicate, foreign IDs и withheld-terminal timeout. Точный кадр из capture воспроизводить неизменённым; для искусственной задержки/дублирования явно записывать transform. Если replay показывает неверную корреляцию или ранний успех, остановить дальнейшие live циклы. Для partial/error использовать прежний raw кадр только если он реально найден и проверен; иначе — явно synthetic replay, без заявления о live-валидации.

Порядок: offline `reqId` gate → при провале остановиться на offline исправлении → включить capture в существующем pump → цикл 1 open/close → replay его raw кадров → только если gate, journal, replay и flat-check прошли, циклы 2 и 3. Перед каждым циклом passively собирать существующие signal→send маркеры; не вводить новый performance gate и не трактовать прежние 2.303/2.830 ms как норматив.

## 6. VPS и хранение

- **Редактирование:** этот plan обновлён в основной локальной копии и скопирован в isolated patch checkout. Runtime patch не выкладывался. Передача исходников на VPS не выполнена: automatic approval review отклонил именно передачу исходников из-за недостаточной подтверждённой авторизации на такую передачу; это не отменяет уже данное live Bybit+OKX разрешение.
- **Будущее выполнение:** только выделенный VPS-контур Bybit+OKX после отдельно организованного переноса нужного кода и read-only preflight. Этот запрос — только дизайн; ордера и сеть сейчас не запускались.
- **Runtime логи/журнал:** VPS-local `BBOT_DATA_ROOT/theta_trades/...` и `BBOT_PRIVATE_DATA_ROOT/wire/...`; файлы впервые материализуются на VPS-local filesystem.
- **Проверка локального сохранения:** прочитать записи из тех же VPS-local roots и сверить journal rows, capture completeness и cycle IDs. Это подтверждает только локальное сохранение/чтение в этом контуре.
- **Удалённый backup:** в эксперимент не входит и не проверяется; его наличие не требуется для утверждения о VPS-local readback.

Существующие signal→send timestamps собирать пассивно. Время сигнала/отправки/ACK/socket arrival/app consumption — локальная хронология; биржевое execution time хранить отдельно. Не чистить старые логи и журналы. При неполном цикле зафиксировать последнее подтверждённое состояние в журналах и остановить кампанию.

## 7. Критерии успеха

- Offline `reqId`-only stale reject gate не приписывает старый ACK текущей заявке.
- Для каждого завершённого цикла обе ноги проходят текущую ID/status/full-qty/positive-finite-price проверку; ACK остаётся отдельным событием.
- Manager записывает `open`, возвращается в watcher, принимает close decision через 10-секундный input, записывает `closed`, а следующий random choice появляется только после read-only flat/no-open-orders подтверждения.
- Для открытия каждой ноги штатный mapper даёт актуальный $7–15 nominal; finite candidate pool проходит однократный preflight без бесконечного redraw или повышения nominal. Close reduce-only использует фактическую filled quantity и завершается даже если её текущий долларовый эквивалент выше $15.
- Все три цикла (если каждый прошёл preflight) используют штатный chooser и qty mapper; выбранная монета может повториться.
- Полный capture позволяет replay настоящих success frames и явно помеченных transforms; timeout/partial/error не называются live-валидированными без raw evidence.
- VPS-local journal/wire данные читаются обратно из тех же локальных roots. Remote backup остаётся непроверенным.
- Любая live неполнота или асимметрия останавливает всю кампанию без повторного ордера, автозакрытия или следующего coin.

## 8. Рекомендуемый следующий шаг

Довести только offline `reqId` gate до однозначного результата, затем подготовить single-reader capture и bounded manager input для трёх циклов с 10-секундным close trigger. Перед будущим запуском проверить eligible pool, mapper result, текущие цены/instrument rules и flat/no-open-orders. В этом ходе выполнена только правка плана; тесты и live/network действия не запускались.
