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

Исторические изменения synthetic-roll size context ограничены `synthetic_roll`; общий OKX book parser остаётся в contract units. Текущий Canary29 `terminal_private` — отдельный явный opt-in внутри существующего `gear22_live_canary` runtime, с общей pre-send size/depth проверкой и теми же private send helpers. Другие режимы исполнения не переключаются автоматически.

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

### Completed live campaign

The authorized 29-coin campaign completed ten open → terminal close → REST-flat cycles on 2026-10-05 and stopped flat at its built-in cap. The full cycle table, native queue/send timing, execution timestamp availability, vector coverage, tested source hash, and VPS-local artifact paths are recorded in [the Canary29 run report](canary29-live-run-2026-10-05.md). Runtime logs and journals first materialized under `/root/b-private-b-exp/response-manager/20261005T-canary29-rerun2/`; remote/mounted durability was not tested.

The live run used source commit `5cd295e` and the deployed `app/bot/runtime.py` SHA-256 recorded in that report. Two later log-only observability edits were compiled and tested after this campaign; they were not included in its ten cycles, but were subsequently deployed for the separate Gear 2.2-policy run documented below. No service configuration was changed by either campaign.

The completed run is evidence for this bounded campaign only. It does not authorize another live campaign, leverage mutation, service restart, or a new destination path. Any later live run needs its own explicit authorization.

Новый режим остаётся в существующем `BotRuntime` и существующем Gear 2.2 one-second feature/vector path. `BBOT_THETA_EXECUTION=terminal_private` разрешён только для `BBOT_PROFILE=gear22_live_canary`, `BBOT_MODE=policy`, `BBOT_THETA_LIVE_SEND=1`, `BBOT_BROKER=private_live`, `VENUE=live`, `LIVE_ORDERS=1`, и включённых floor/TW-p50/theta watchers. `BBOT_THETA_TRADE=0` завершает startup до private/account work. `BBOT_THETA_POLICY=gear22` использует существующую Gear22 policy; `synthetic` включает отдельный односекундный Canary29 roll. Синтетический выбор потребляет один RNG draw на общем tick, включая pending hold; close использует только фактическую открытую позицию и roll 31. Вектор остаётся общим и должен быть ready для open.

Execution selector отделён от policy selector. Оба решения проходят общий `ThetaTradeManager` size/depth gate и тот же injected private sender. Синхронное ожидание private terminal fill уходит в один заранее прогретый worker; K=1 reservation ставится до scheduling. Tick loop продолжает policy hold без backlog и второго send. Свежесть проверяет общий `TickValidityGate`, включая generation после reconnect и возраст 0–2000 ms. Закрытие подтверждает depth по фактическим quantities обеих ног. Любой неопределённый исход после возможной отправки сохраняет pending и останавливает кампанию. До send stale/invalid/depth reject безопасно пропускает текущий tick без fallback.

Настройка плеча и её REST readback выполняются отдельной разрешённой prep-стадией для новых инструментов. Она записывает подтверждённый полный пул в `BBOT_CONFIRMED_1X_COINS`; runtime требует подтверждения для каждого активного coin, строит 58-entry cache и не вызывает leverage setter. Перед стартом runtime делает один accountwide flat snapshot (Bybit position/open orders с `settleCoin=USDT`, paginated; OKX SWAP position/open orders) и фильтрует активные инструменты. После каждого close он опрашивает только закрытый symbol максимум 5 секунд. API errors/malformed snapshots не считаются flat. Startup отказывает при непустой journal position.

Сигнал получает локальные wall-ms и `monotonic_ns` сразу после выбора decision, до size/freshness/private gates. Оба значения передаются через intent `extra` в первую `signal_decision` строку существующего `StepChrono`; venue execution timestamps остаются отдельными.

Для будущей отдельно авторизованной кампании сверить exact source tree/hash перед запуском; не передавать env, private/runtime data или новые файлы в code tree. Текущая кампания использовала отдельный run directory под `/root/b-private-b-exp/response-manager/`; mounted/remote durability не подтверждена. Повторное применение службы, изменение production D или её конфигурации не выполнялось.

Изолированному pre-B2.2 checkout требуется ровно один прежний source dependency, который `LiveFloorObserver` открывает file-relative: `research/gear22_quiet_regime_viz/floors.py`. Он совпадает с проверенным would-send source SHA-256 `9ba5e76e5abb7c3ece2033c6ffb007d50af434069a5339ca2dce99f76d573ef0`; локальный tracked source сохранён в checkout, а VPS копия положена в ту же package-relative path. Код чистая floor math dependency (stdlib + numpy); source fallback checkout остаётся вторым на `PYTHONPATH`.

Leverage prep command (no trading loop; old trio is not GET-read again):

```bash
cd /root/b-private-b-exp/response-manager-code/response-handler-20261005
PYTHONPATH=/root/b-private-b-exp/response-manager-code/response-handler-20261005:/root/spread_bbot_would_send_prod \
  /root/venv/bin/python validation/run_response_manager_experiment.py --prepare-canary29
```

It sets and reads back only the 26 newly authorized instruments. It writes a non-secret confirmation report and `confirmed_1x.env` under its own prep run root. For the campaign, make a separate run root under `/root/b-private-b-exp/response-manager/`, copy `/data/bbot-would-send-prod/state/floor_warm.pkl` into that run's `data/state/floor_warm.pkl` (read-only source), then load the two `BBOT_*COINS` values from the prep config. Export the flags above, `BBOT_DATA_ROOT=$RUNROOT/data`, `BBOT_PRIVATE_DATA_ROOT=$RUNROOT/private-data`, `BBOT_FLOOR_WARM=1`, `BBOT_FLOOR_WARM_PATH=$RUNROOT/data/state/floor_warm.pkl`, and `BBOT_PRIVATE_ENV_FILE=/etc/spread/bbot-private-live.env`; run `/root/venv/bin/python -m app.bot` from the approved code directory with that directory first on `PYTHONPATH` and `/root/spread_bbot_would_send_prod` second for research fallback. The runtime startup performs its one fresh accountwide flat snapshot after prep and before any signal send; do not run a separate account baseline audit.

Локальная scoped-проверка для текущих контрактов: `py_compile` по изменённым Python модулям, `tests.test_bbot_theta_trade_k1.TerminalExecutionModeTests`, Canary29 policy contract tests, и $7–$15 shared notional cases. Не повторять старые private handler/replay suites, если изменённые интерфейсы их не затрагивают. Canary лимит — 10 только полных open→terminal close→REST flat циклов. Нет таймера принудительного close; close только по roll 31 и общей policy/state. После 10-го flat — остановка. Любое расхождение/unknown exposure — halt, без автоматического retry/recovery/flatten.

The separate Gear 2.2-policy canary was observed at 2026-10-05 14:33:40 UTC: its original PID still matched the isolated runroot, with one local RVN long slot, no pending intent, zero completed flat cycles, and no halt marker. Current quantities and journal fill prices, along with the observation limits, are recorded in [the Gear 2.2 canary status report](gear22-live-canary-run-2026-10-05.md). That original process was launched with the earlier ten-cycle cap. The reviewed long-run replacement configuration below uses `BBOT_CANARY_MAX_CYCLES=0` and a 72-hour open window; the source snapshot and position-preserving restart condition are recorded below. There is no forced-close timer. No claim is made about mounted/remote durability or unattended recovery behavior.

## Long-running Gear 2.2 canary implementation (local review only)

The isolated source now supports `BBOT_CANARY_MAX_CYCLES=0` for no cycle cap and `BBOT_CANARY_OPEN_WINDOW_HOURS=72` for the entry window. The default cap remains 10. At the deadline, new opens are blocked while an existing position continues through its ordinary Gear 2.2 close; the process stops only after close plus REST-flat confirmation. It never closes by timer. The heartbeat and lifecycle writer persist `canary_state.json` atomically under the new run data root, with the real policy selector (`gear22` or `synthetic`), `terminal_private` execution selector, current slot/pending/halt, completed flat cycles, deadline, and last policy-evaluation timestamp. A checkpoint error halts with pending retained. This file is local operational state, not an account ledger or mounted-storage durability guarantee.

Restarting with an existing position requires an explicit `BBOT_CANARY_RESUME_MANIFEST`. Resume is limited to a known fully terminal open: verify the source PID is gone; manifest, heartbeat `OpenPosition`, and terminal-open trade row agree; replay all trade-journal partitions and reject any later close, overlap, pending, abort, or unknown outcome; confirm contract-to-base quantities; then take one fresh accountwide Bybit linear-USDT and OKX SWAP positions/orders snapshot. Adoption requires exactly the matched position on both venues, no active orders, and no other nonzero positions. The frozen policy spread is carried from the source heartbeat/checkpoint rather than recomputed from venue execution prices. Any missing, malformed, stale, or mismatched evidence refuses startup. Do not use this path for an after-send unknown/asymmetric outcome, and do not auto-recover or flatten.

At 2026-10-05 15:22:08 UTC, one read-only VPS snapshot found original PID `2153180` still running `/root/venv/bin/python -m app.bot` (elapsed 4,412 seconds) from `/root/b-private-b-exp/response-manager/20261005T140836Z-gear22-canary/`. The latest heartbeat at 15:21:57.039 UTC showed the same local RVN long slot and `pending=False`; the one partition had exactly five rows for that intent (pending, three venue messages, terminal open), zero close rows, zero `canary29_cycle_flat` markers, and zero halt markers. The open row and heartbeat agree on 374 OKX contracts / 3,740 Bybit units; the heartbeat retains frozen `fill_spread_pp=1.1219147344801823`. The startup log says `theta_live_send=off`; this is the previously identified misleading manager-flag log, not evidence that the terminal private sender is stopped. This read did not query exchange APIs, so fresh venue state remains unverified until the new runtime's one required accountwide startup snapshot.

The intended preserved-open resume deadline is `2026-10-08T14:08:36Z`, measured from the original run start, with `max_cycles=0` and `open_window_hours=72`. The candidate manifest is staged locally at `/private/tmp/gear22-longcanary-resume-manifest-planned.json` for root review; it is not yet valid for launch because the old PID is still alive. After an authorized graceful stop, re-read only that process's final heartbeat and append-only lifecycle rows, verify no intervening close/open/abort, then update the manifest from those final values and confirm the PID is gone. The new runtime independently takes one fresh accountwide positions/orders snapshot and refuses any mismatch. If the source closes before handoff, omit the resume manifest and require the normal flat startup gate instead.

An empty slot is serialized as `position: {}`; this describes only the local manager slot and never substitutes for the startup account-wide exchange snapshot. Resume also conservatively refuses a source log containing the generic `canary29_stopped` marker, even if that marker came from an ordinary flat stop; in that case use the flat-start path only after exchange state is confirmed flat.

The previous process was gracefully stopped after a final terminal-open source snapshot, then the reviewed long-run build started as PID `2191957` under `/root/b-private-b-exp/response-manager/20261005T194200Z-gear22-longrun/`. At 2026-10-05 19:47:03 UTC it was alive and ready; its one built-in startup account-wide positions/orders check passed. The new atomic checkpoint at 19:46:55.249 UTC records Gear 2.2 policy, `terminal_private`, RVN long 404 OKX contracts / 4,040 Bybit units, `pending=false`, one previously completed flat cycle, no halt, `max_cycles=0`, and the original 72-hour deadline `2026-10-08T14:08:36Z`. The adopted open provenance points to the prior run's `theta_trades/event_date=2026-10-05/trades.jsonl`; no new-run open trade row should appear for an adopted position. Its latest policy evaluation at 19:46:54.486 UTC was the ordinary `hold_below_min_theta` action. Current venue state after startup has not been re-queried.

The source process was PID 2153180 and its final open was terminally recorded; this handoff was permitted only after the source PID exited, the final journal/heartbeat matched, and the new runtime's one fresh account-wide snapshot accepted the exact position with no active orders or unexplained positions. The new process has no cycle cap and blocks new entries after the original 72-hour window; an open position can close only through ordinary Gear 2.2 policy. There is no forced close, automatic recovery, or supervisor. Runtime data/logs first materialize in the new VPS runroot; remote/mounted durability is unverified. See [the live run report](gear22-live-canary-run-2026-10-05.md) and [the unit monitor specification](gear22-grok-monitor-spec.md) for the timestamped status and monitoring rules.
