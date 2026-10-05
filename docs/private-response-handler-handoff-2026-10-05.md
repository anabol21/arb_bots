# B-private response handler — контекст для следующего агента

## Быстрый вход

Исторические три цикла описаны ниже. Текущая реализация Canary29 ведётся отдельно в `/private/tmp/arb_bots-canary29-20261005`, ветка `codex/canary29-terminal-policy`, от опубликованного pre-B2.2 commit `ba3a98f2f8e9253988ef68de8ac9445e3e380397`. Исходный пользовательский checkout не менялся. Эти контексты различны: локальный checkout, отдельно подготовленный VPS код, экспериментальный процесс и действующие VPS units.

Перед правками прочитайте [архитектуру](../architecture.md), затем этот список:

- `app/bot/runtime.py` — synthetic-roll startup/warmup и runtime quote context;
- `app/bot/theta_trade_manager.py` — size gate, roll state и journal decisions;
- `app/bot/private/place_send.py` — sizing, WS place, terminal response wait и close;
- `app/bot/private/send_legs.py`, `ws_warm_loop.py` — warmed dual sender и очереди;
- `app/bot/private/wire_transcript.py` — redacted private response capture;
- `app/bot/private/coin_qty.py` — OKX contract ↔ coin units;
- `tests/test_bbot_synthetic_roll_private.py`, `test_bbot_synthetic_roll.py`, `test_wire_transcript.py`, `test_warm_single_loop.py`, `test_okx_depth_units.py` — scoped regressions;
- `validation/run_response_manager_experiment.py` — только bounded experiment runner, не сервис.

Торговое поведение и добавление OKX depth size context ограничены `synthetic_roll`. При этом изменены общие private send/capture helpers (`send_legs.py`, `ws_warm_loop.py`, `wire_transcript.py`), используемые этим профилем. `gear22_live_canary` и остальные контуры сохраняют прежний runtime size context. Парсер OKX книг остаётся в contract units.

## Инварианты и границы

- Place ACK означает принятие запроса, не fill. Торговое состояние переходит дальше только по соответствующему terminal order update обеих площадок.
- OKX fill должен быть `state=filled`, с положительными конечными `accFillSz` и `avgPx`; Bybit — `orderStatus=Filled`, с `cumExecQty` и `avgPrice`. Накопленный fill должен равняться отправленному количеству.
- Корреляция использует client order ID попытки и ID, возвращённые её ACK. Идентифицируемые ответы другой попытки игнорируются.
- Промежуточные working/partial/execution-fragment updates не завершают и сами по себе не останавливают ожидание terminal state. Terminal partial/cancel, reject, несовпадение quantity/price после send или timeout останавливают roll и оставляют возможную exposure pending. Нехватка depth до отправки блокирует отправку и сама по себе не создаёт pending exposure.
- На старте профиль подготавливает и кеширует dual sender до signal-facing tasks; обе очереди готовы без dummy order/frame. Runtime place path не делает REST-запросов и не отправляет retry/recovery orders. Внешний bounded experiment отдельно читал счёт для flat projection.
- Matching terminal Filled может прийти до ACK, если текущий client ID уже известен. ACK подтверждает принятие заявки, но не fill и не завершает trade.
- Close использует сохраненные фактические open fills и `reduceOnly=true`; объём не пересчитывается от новой целевой суммы.
- OKX `books5` depth — число контрактов; `ctVal` переводит его в base coin. Bybit depth уже в base coin. Отсутствующий/невалидный `ctVal` закрывает gate.
- Размеры обоих отправляемых legs должны представлять один и тот же coin qty после округления по `lotSz`/`qtyStep`.
- WS parsing и signal policy заморожены. Этот patch не меняет collector, service manager, live units или установку кода.

## Что фактически подтверждено

Результат трёх bounded циклов: 2Z, 2Z, LA; каждый открыл и закрыл обе ноги, всего 6 dual-leg intents / 12 order requests. После каждого close подтверждён flat projection. На первом цикле replay переставленных terminal updates, duplicate ACK, чужих ID и withheld terminal прошёл для обеих площадок. Это live evidence конкретного VPS-эксперимента, не доказательство надёжности других инструментов, длительного unattended запуска, доходности или durable remote storage.

Send timing в отчёте оценён: signal имеет wall timestamp с миллисекундной точностью, его отображали на локальную monotonic шкалу по anchor того же процесса. Execution timing использует venue `execTime`/`fillTime`; не измерены host/venue clock offset и локальная receipt latency не равна exchange execution latency. Не описывать эти значения как точные native monotonic signal-to-send или как независимую оценку биржевых часов.

Подробные факты, таблицы, run IDs и ограничения: [итог bounded size-gate/live experiment](size-gate-live-experiment-2026-10-05.md). Более ранний [отчёт первого запуска](response-manager-live-experiment-2026-10-05.md) завершился до отправки заявок и superseded успешным отчётом; он полезен только для истории единиц измерения. [План эксперимента](response-handler-experiment-plan-2026-10-04.md) и [раннее описание response handling](synthetic-roll-response-handling-2026-10-04.md) — исторические design/implementation notes; при расхождении ориентироваться на успешный отчёт и текущий код.

Важно для тестов: основная старая фикстура в `test_bbot_synthetic_roll_private.py` — синтетическая field-level структура, реконструированная по раннему B0 отчёту, а не исходные сырые wire frames. Новый live first-cycle capture/replay описан в успешном отчёте; не приписывать старой фикстуре происхождение от финального запуска.

## Где выполнялся запуск и где лежат данные

- Код редактировался: изолированный checkout, указанный выше.
- Эксперимент выполнялся на VPS `/root/venv/bin/python` из `/root/b-private-b-exp/response-manager-code/response-handler-20261005/`.
- Run: `20261004T222519Z-response` (UTC; 2026-10-05 Moscow date).
- Результаты и первые локальные журналы: `/root/b-private-b-exp/response-manager/20261004T222519Z-response/`, включая `result.json`, `data/theta_trades/event_date=2026-10-04/step_chrono.jsonl` и `private-data/`.
- Приложение файлов в отчёте означает VPS-local materialization. Копия на mounted/remote durable storage не проверялась.
- Секретный env-файл существует по пути `/etc/spread/bbot-private-live.env`; в документах и коммитах только путь, содержимое никогда не копировать.

В предварительной подготовке было подтверждено 1x leverage для трёх инструментов на обеих площадках (шесть setup POSTs). Финальный runner использовал это подтверждение без повторных setters и account/config GETs. Его preflight намеренно специфичен этому эксперименту и не является общей startup/account verification гарантией.

В текущем checkout добавленный regression `test_non_synthetic_live_theta_does_not_attach_private_size_gate` пропущен unittest runner, потому что отсутствует зависимость `websockets`. Проверка кода подтверждает, что private size marker теперь добавляется только при `profile == synthetic_roll`; no-send runner self-test прошёл (`orders_sent=0`). Это не утверждение, что пропущенный unit test исполнился.

## Следующая работа

Сначала изучить diff и свежие тесты только для изменённых контрактов. Не повторять успешную торговую кампанию, GET-аудит или изменение leverage без новой явной авторизации. Нерешённые отдельные темы: долгий unattended soak, crash/restart/recovery semantics для pending exposure и сохранность журналов при сбое локального/VPS storage и при удалённом копировании. Прежде чем менять send/recovery topology, сравнить варианты и обновить архитектуру.

Эти отчёты и документы передают технический контекст, но не дают автоматического разрешения на новые live заявки, VPS deployment, service restart или remote-storage изменения.

## Canary29 implementation handoff

Новый режим остаётся в существующем `BotRuntime` и существующем Gear 2.2 one-second feature/vector path. `BBOT_THETA_EXECUTION=terminal_private` разрешён только для `BBOT_PROFILE=gear22_live_canary`, `BBOT_MODE=policy`, `BBOT_THETA_LIVE_SEND=1`, `BBOT_BROKER=private_live`, `VENUE=live`, `LIVE_ORDERS=1`, и включённых floor/TW-p50/theta watchers. `BBOT_THETA_TRADE=0` завершает startup до private/account work. `BBOT_THETA_POLICY=gear22` использует существующую Gear22 policy; `synthetic` включает отдельный односекундный Canary29 roll. Синтетический выбор потребляет один RNG draw на общем tick, включая pending hold; close использует только фактическую открытую позицию и roll 31. Вектор остаётся общим и должен быть ready для open.

Execution selector отделён от policy selector. Оба решения проходят общий `ThetaTradeManager` size/depth gate и тот же injected private sender. Синхронное ожидание private terminal fill уходит в один заранее прогретый worker; K=1 reservation ставится до scheduling. Tick loop продолжает policy hold без backlog и второго send. Свежесть проверяет общий `TickValidityGate`, включая generation после reconnect и возраст 0–2000 ms. Закрытие подтверждает depth по фактическим quantities обеих ног. Любой неопределённый исход после возможной отправки сохраняет pending и останавливает кампанию. До send stale/invalid/depth reject безопасно пропускает текущий tick без fallback.

Настройка плеча и её REST readback выполняются отдельной разрешённой prep-стадией для новых инструментов. Она записывает подтверждённый полный пул в `BBOT_CONFIRMED_1X_COINS`; runtime требует подтверждения для каждого активного coin, строит 58-entry cache и не вызывает leverage setter. Перед стартом runtime делает один accountwide flat snapshot (Bybit position/open orders с `settleCoin=USDT`, paginated; OKX SWAP position/open orders) и фильтрует активные инструменты. После каждого close он опрашивает только закрытый symbol максимум 5 секунд. API errors/malformed snapshots не считаются flat. Startup отказывает при непустой journal position.

Сигнал получает локальные wall-ms и `monotonic_ns` сразу после выбора decision, до size/freshness/private gates. Оба значения передаются через intent `extra` в первую `signal_decision` строку существующего `StepChrono`; venue execution timestamps остаются отдельными.

Перед любым запуском сверить exact source tree/hashes в уже одобренной директории `/root/b-private-b-exp/response-manager-code/response-handler-20261005/`; не передавать env, private/runtime data или новые файлы вне неё. Runtime/data и свежие журналы Canary пишутся под `/root/b-private-b-exp/response-manager/` в отдельный run directory; mounted/remote durability не подтверждается. Повторное применение службы, изменение production D или её конфигурации исключено.

Локальная scoped-проверка для текущих контрактов: `py_compile` по изменённым Python модулям, `tests.test_bbot_theta_trade_k1.TerminalExecutionModeTests`, Canary29 policy contract tests, и $7–$15 shared notional cases. Не повторять старые private handler/replay suites, если изменённые интерфейсы их не затрагивают. Canary лимит — 10 только полных open→terminal close→REST flat циклов. Нет таймера принудительного close; close только по roll 31 и общей policy/state. После 10-го flat — остановка. Любое расхождение/unknown exposure — halt, без автоматического retry/recovery/flatten.

Эта реализация ещё не подтверждает VPS deployment или live readiness. До запуска остаются code deploy и exact-hash verification в разрешённом каталоге, read-only выбор активного would-send pool/warm state, передача prep/launch команд root, и root clearance для последующего live stage. Не выставлять плечо и не отправлять заявки до этого clearance.
