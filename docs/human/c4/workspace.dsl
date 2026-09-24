# Lite 2025.11.08 rejects containerDb inside group. Stores stay in groups as container + tags "Database" (cylinder style below). Do not restore containerDb.
# Component views: click a process container on a Contour view to open Code — <human block name>.
workspace "arb_bots — human contours" "Six process contours as coded. Human diagram layer. Not a running topology." {

    !impliedRelationships false

    model {
        human = person "Человек" "Смотрит журналы, включает юниты, запускает прогон истории."

        okxPublic = softwareSystem "OKX — публичные цены" "Книги заявок и список инструментов. Без ключей." {
            tags "External"
        }
        bybitPublic = softwareSystem "Bybit — публичные цены" "Книги заявок и список инструментов. Без ключей." {
            tags "External"
        }
        okxOrders = softwareSystem "OKX — приём ордеров" "Торговый канал. Только контур отправки." {
            tags "External"
        }
        bybitOrders = softwareSystem "Bybit — приём ордеров" "Торговый канал. Только контур отправки." {
            tags "External"
        }
        remoteCopy = softwareSystem "Удалённая копия" "rclone. Не первая запись на сервере." {
            tags "External"
        }

        arbBots = softwareSystem "arb_bots" "Сбор цен, решение, журнал, отправка ордера, прогон истории." {

            group "Contour — Live prices" {
                liveCollector = container "Сбор цен" "spread-collector.service · app/screaner_b_o.py" "Python" {
                    lcPairs = component "Список пар" "CSV; см. глоссарий: take=yes" "app/utils/universe_csv.py"
                    lcWs = component "Приём котировок" "async, заморожено" "app/screaner_b_o.py"
                    lcReconnect = component "Переподключение" "async, не разбор книг" "app/utils/ws_reconnect.py"
                    lcSpread = component "Разбор и спред" "sync, заморожено" "app/screaner_b_o.py"
                    lcEnqueue = component "Постановка на запись" "async, очередь внутри процесса" "app/screaner_b_o.py"
                    lcWrite = component "Запись parquet" "поток-worker; см. глоссарий: parquet" "app/storage/writer.py"
                    lcSchema = component "Схема тика" "колонки lean-тика" "app/schema/lean_event.py"
                    lcSpoolRec = component "Запас и повтор" "sync файл + поток повтора; см. глоссарий: spool" "app/storage/spool.py · app/storage/recovery.py"
                    lcGapsMod = component "Журнал пропусков" "sync jsonl" "app/utils/ws_gap_journal.py"
                    lcBarsOff = component "Запись баров (флаг выключен)" "код есть; в юните SPREAD_COLLECT_BARS=0; кто пишет /data/bars на VPS — неизвестно" "app/screaner_b_o.py"
                }
                livePubQueue = container "Очередь записи" "очередь внутри сборщика; worker пишет файлы" "queue.Queue" {
                    tags "Queue"
                }
                liveTicks = container "Тики" "первая запись /data/live" "parquet" {
                    tags "Database"
                }
                liveSpool = container "Запас при сбое записи" "/data/spool" "spool" {
                    tags "Database"
                }
                liveGaps = container "Пропуски связи" "/data/gaps" "jsonl" {
                    tags "Database"
                }
                liveCompactor = container "Уплотнение тиков" "spread-compactor.timer · app.storage.compactor" "Python oneshot" {
                    lcompMerge = component "Слияние окон тиков" "oneshot, sync чтение" "app/storage/compactor.py"
                    lcompWrite = component "Запись уплотнённых тиков" "sync parquet" "app/storage/compactor.py"
                }
                liveCompacted = container "Уплотнённые тики" "/data/compacted" "parquet" {
                    tags "Database"
                }
                liveBackup = container "Копия тиков" "spread-backup-transfer.timer · app.storage.backup_transfer" "Python oneshot" {
                    lbakPick = component "Выбор готовых файлов" "oneshot, sync" "app/storage/backup_transfer.py"
                    lbakSend = component "Отправка копии" "ждёт rclone" "app/storage/backup_transfer.py"
                    lbakManifest = component "Манифест отправленных" "sqlite рядом с копией" "app/storage/backup_transfer.py"
                }
                liveBars = container "Бары (писатель неизвестен)" "/data/bars; боевой сборщик бары не пишет" "parquet" {
                    tags "Database"
                }
                liveBarsCompactor = container "Уплотнение баров" "spread-bars-compactor.timer · app.storage.bars_compactor" "Python oneshot" {
                    lbarcMerge = component "Слияние окон баров" "oneshot, sync; не удаляет источник до копии" "app/storage/bars_compactor.py"
                    lbarcSchema = component "Схема бара" "bar_5m" "app/schema/lean_event.py"
                }
                liveBarsCompacted = container "Уплотнённые бары" "/data/bars_compacted_v2" "parquet" {
                    tags "Database"
                }
                liveBarsBackup = container "Копия баров" "таймеры копии баров · app.storage.backup_transfer" "Python oneshot" {
                    lbarbHive = component "Копия улья баров" "тот же модуль, --layout hive" "app/storage/backup_transfer.py"
                    lbarbSend = component "Отправка копии баров" "ждёт rclone" "app/storage/backup_transfer.py"
                }
            }

            group "Contour — Canary prices" {
                canaryDiscovery = container "Поиск новых пар" "spread-discovery-hotadd-canary.timer · python -m app.discovery" "Python oneshot" {
                    cdRest = component "Запрос списков инструментов" "REST, sync; PR #53, нет на main" "app/discovery/intersection.py"
                    cdIntersect = component "Пересечение пар" "не переписывает боевой CSV; PR #53" "app/discovery/intersection.py"
                    cdDelta = component "Запись списка новинок" "атомарный delta; PR #53" "app/utils/universe_delta.py"
                    cdMain = component "Точка входа поиска" "python -m app.discovery; PR #53" "app/discovery/__main__.py"
                }
                canaryDelta = container "Список новинок" "delta-файл; не боевой список пар" "csv" {
                    tags "Database"
                }
                canaryDrop = container "Список снятия" "drop-файл" "csv" {
                    tags "Database"
                }
                canaryCollector = container "Сбор цен (изолированный)" "spread-collector-hotadd-canary.service · app/screaner_b_o.py, другой каталог" "Python" {
                    ccPairs = component "Список пар (изолированный)" "10 строк take=yes; см. глоссарий: take=yes" "app/utils/universe_csv.py"
                    ccHotAdd = component "Опрос новинок и снятия" "async опрос файлов; не REST; PR #53" "app/utils/hot_add.py"
                    ccTasks = component "Набор слушателей" "можно добавить задачу после старта; PR #53" "app/utils/task_supervisor.py"
                    ccWs = component "Приём котировок (изолированный)" "async, тот же скрипт, другой каталог" "app/screaner_b_o.py"
                    ccSpread = component "Разбор и спред (изолированный)" "sync, заморожено" "app/screaner_b_o.py"
                    ccEnqueue = component "Постановка на запись (изолированная)" "async" "app/screaner_b_o.py"
                    ccWrite = component "Запись parquet (изолированная)" "не /data/live" "app/storage/writer.py"
                    ccSpoolRec = component "Запас и повтор (изолированные)" "свой spool; см. глоссарий: spool" "app/storage/spool.py · app/storage/recovery.py"
                    ccGapsMod = component "Журнал пропусков (изолированный)" "свой gaps" "app/utils/ws_gap_journal.py"
                }
                canaryPubQueue = container "Очередь записи (изолированная)" "очередь внутри изолированного сборщика" "queue.Queue" {
                    tags "Queue"
                }
                canaryTicks = container "Тики (изолированные)" "/data/live-hotadd-canary" "parquet" {
                    tags "Database"
                }
                canarySpool = container "Запас (изолированный)" "/data/spool-hotadd-canary" "spool" {
                    tags "Database"
                }
                canaryGaps = container "Пропуски (изолированные)" "/data/gaps-hotadd-canary" "jsonl" {
                    tags "Database"
                }
            }

            group "Contour — Stub B" {
                stubBot = container "Решение без отправки" "spread-bbot.service · python -m app.bot" "Python" {
                    sbLoop = component "Цикл процесса" "async; python -m app.bot" "app/bot/runtime.py"
                    sbBooks = component "Приём котировок" "async, свои книги, не сборщик" "app/bot/ws_books.py"
                    sbDecide = component "Решение" "sync, без I/O" "app/policy/trade_manager.py"
                    sbStub = component "Заглушка без отправки" "нет приватных сокетов; см. глоссарий: stub" "app/bot/stub_broker.py"
                    sbJournal = component "Журнал намерений" "jsonl; см. глоссарий: would_send" "app/bot/journal.py"
                }
                stubJournal = container "Журнал намерений" "/data/bbot" "jsonl" {
                    tags "Database"
                }
                stubBackup = container "Копия журнала" "spread-bbot-backup-transfer.timer · app.bot.backup" "Python oneshot" {
                    sbbGuard = component "Проверка путей копии" "отказ писать деревья сборщика" "app/bot/backup.py"
                    sbbSend = component "Отправка копии журнала" "rclone, префикс spread-bbot" "app/bot/backup.py"
                }
                gear2Bot = container "Решение без отправки (4 монеты)" "spread-bbot-gear2.service · python -m app.bot" "Python" {
                    g2Loop = component "Цикл процесса (4 монеты)" "async; тот же вход" "app/bot/runtime.py"
                    g2Books = component "Приём котировок (4 монеты)" "async" "app/bot/ws_books.py"
                    g2Decide = component "Решение по рынку" "sync, слот на весь процесс" "app/policy/gear2_market_manager.py"
                    g2Stub = component "Заглушка без отправки (4 монеты)" "см. глоссарий: stub" "app/bot/stub_broker.py"
                    g2Journal = component "Журнал намерений (4 монеты)" "jsonl" "app/bot/journal.py"
                }
                gear2Journal = container "Журнал намерений (4 монеты)" "/data/bbot-gear2" "jsonl" {
                    tags "Database"
                }
                gear2Backup = container "Копия журнала (4 монеты)" "spread-bbot-gear2-backup-transfer.timer · app.bot.backup" "Python oneshot" {
                    g2bGuard = component "Проверка путей копии (4 монеты)" "отказ писать деревья сборщика" "app/bot/backup.py"
                    g2bSend = component "Отправка копии (4 монеты)" "rclone, префикс spread-bbot-gear2" "app/bot/backup.py"
                }
            }

            group "Contour — Canary B" {
                thetaBot = container "Решение раз в секунду, без отправки" "VPS unit spread-bbot-theta-k1-canary · python -m app.bot · файла юнита нет в git" "Python" {
                    thLoop = component "Цикл процесса (1 Гц)" "async; файла юнита нет в git" "app/bot/runtime.py"
                    thBooks = component "Приём котировок (1 Гц)" "async" "app/bot/ws_books.py"
                    thWatch = component "Наблюдение раз в секунду" "в том же процессе, не отдельный юнит" "app/bot/theta_screener.py"
                    thFloor = component "Запись метрик пола" "jsonl" "app/bot/floor_watcher.py"
                    thTw = component "Запись метрик окна" "jsonl" "app/bot/tw_p50_watcher.py"
                    thDecide = component "Решение раз в секунду" "sync; см. глоссарий: theta" "app/bot/theta_trade_manager.py"
                    thRules = component "Правила прогона" "чистая функция, без I/O" "research/gear22_backtest/policy.py"
                    thJournal = component "Журнал намерений (1 Гц)" "theta_trades jsonl; без отправки" "app/bot/theta_trade_manager.py"
                }
                thetaMetrics = container "Метрики наблюдения" "floor / tw_p50 / theta jsonl" "jsonl" {
                    tags "Database"
                }
                thetaJournal = container "Журнал намерений (1 Гц)" "theta_trades jsonl" "jsonl" {
                    tags "Database"
                }
            }

            group "Contour — Live send" {
                walBot = container "Решение и отправка (2 монеты)" "spread-bbot-canary-wal-eden.service · python -m app.bot" "Python" {
                    walLoop = component "Цикл процесса (2 монеты)" "async, решение по тику рынка" "app/bot/runtime.py"
                    walBooks = component "Приём котировок (2 монеты)" "async, публичные книги" "app/bot/ws_books.py"
                    walDecide = component "Решение по тику (2 монеты)" "sync; WAL и EDEN" "app/policy/gear2_market_manager.py"
                    walGate = component "Проверка отправки" "нужны live и разрешение вместе; см. глоссарий: live send" "app/bot/private/venue.py"
                    walWarm = component "Тёплые торговые сокеты (2 монеты)" "async, один цикл на процесс" "app/bot/private/ws_warm_session.py"
                    walEnq = component "Постановка ордера в очередь" "async; см. глоссарий: Contour B" "app/bot/private/ws_trivial_dual_leg.py"
                    walSend = component "Отправка ордера (2 монеты)" "async ws.send" "app/bot/private/live_broker.py"
                    walAck = component "Ожидание ответа биржи" "не сделка-fill" "app/bot/private/dual_leg_ack.py"
                    walJourn = component "Журнал ордеров (2 монеты)" "jsonl; путь private/wire на VPS неизвестен" "app/bot/journal.py"
                }
                walQueue = container "Очередь ордеров (2 монеты)" "asyncio.Queue внутри процесса" "asyncio.Queue" {
                    tags "Queue"
                }
                walJournal = container "Журнал ордеров (2 монеты)" "/data/bbot-canary-wal-eden" "jsonl" {
                    tags "Database"
                }
                g22Bot = container "Решение и отправка (30 монет)" "spread-bbot-gear22-live-canary.service · python -m app.bot" "Python" {
                    g22Loop = component "Цикл процесса (30 монет)" "async, решение раз в секунду" "app/bot/runtime.py"
                    g22Books = component "Приём котировок (30 монет)" "async" "app/bot/ws_books.py"
                    g22Decide = component "Решение раз в секунду (30 монет)" "может вызвать отправку; см. глоссарий: theta" "app/bot/theta_trade_manager.py"
                    g22Rules = component "Правила (30 монет)" "те же замороженные правила, что у прогона" "research/gear22_backtest/policy.py"
                    g22Gate = component "Проверка отправки (30 монет)" "см. глоссарий: live send" "app/bot/private/venue.py"
                    g22Warm = component "Тёплые торговые сокеты (30 монет)" "async, app/bot/private/ws_warm_loop.py" "app/bot/private/ws_warm_session.py"
                    g22Enq = component "Постановка ордера в очередь (30 монет)" "async, тот же путь отправки" "app/bot/private/ws_trivial_dual_leg.py"
                    g22Send = component "Отправка ордера (30 монет)" "async ws.send" "app/bot/private/live_broker.py"
                    g22Ack = component "Ожидание ответа биржи (30 монет)" "не сделка-fill" "app/bot/private/dual_leg_ack.py"
                    g22Journ = component "Журнал ордеров (30 монет)" "jsonl" "app/bot/journal.py"
                    g22WireMod = component "Журнал провода" "после удачной отправки/приёма; см. глоссарий: wire" "app/bot/private/wire_transcript.py"
                }
                g22Queue = container "Очередь ордеров (30 монет)" "asyncio.Queue внутри процесса" "asyncio.Queue" {
                    tags "Queue"
                }
                g22Journal = container "Журнал ордеров (30 монет)" "/data/bbot-gear22-live-canary" "jsonl" {
                    tags "Database"
                }
                g22Wire = container "Журнал провода" "private/wire jsonl" "jsonl" {
                    tags "Database"
                }
            }

            group "Contour — Simulator" {
                simReplay = container "Прогон истории" "model.ipynb / research/gear22_backtest/replay.py · без systemd" "Python offline" {
                    simNb = component "Ноутбук модели" "офлайн; см. глоссарий: gear" "model.ipynb"
                    simFeat = component "Сбор признаков" "1 Гц таблица с диска, не живой сборщик" "research/gear22_feature_day_probe.py"
                    simRules = component "Правила прогона" "чистая функция" "research/gear22_backtest/policy.py"
                    simRun = component "Прогон раз в секунду" "fill = spread_last; см. глоссарий: spread_last" "research/gear22_backtest/replay.py"
                    simKnobs = component "Замороженные пороги" "не живой бот" "research/gear22_backtest/params_frozen.py"
                }
                histTicks = container "Исторические тики" "parquet с диска; не живой сборщик" "parquet" {
                    tags "Database"
                }
                simFeatures = container "Таблица признаков" "1 Гц, из исторических тиков" "table" {
                    tags "Database"
                }
                simTrades = container "Сделки прогона" "не ордера на бирже" "table" {
                    tags "Database"
                }
            }
        }

        # Level 1 — capabilities of the system as a whole, not one running contour.
        okxPublic -> arbBots "котировки (async)" "WebSocket" {
            tags "async"
        }
        bybitPublic -> arbBots "котировки (async)" "WebSocket" {
            tags "async"
        }
        okxPublic -> arbBots "список пар" "REST"
        bybitPublic -> arbBots "список пар" "REST"
        arbBots -> okxOrders "отправка ордера (async)" "WebSocket" {
            tags "async"
        }
        arbBots -> bybitOrders "отправка ордера (async)" "WebSocket" {
            tags "async"
        }
        arbBots -> remoteCopy "копия (async)" "rclone" {
            tags "async"
        }
        human -> arbBots "журналы, юниты, прогон"

        # Contour — Live prices
        okxPublic -> liveCollector "котировки (async)" "WebSocket" {
            tags "async"
        }
        bybitPublic -> liveCollector "котировки (async)" "WebSocket" {
            tags "async"
        }
        liveCollector -> livePubQueue "батч (async)" {
            tags "async"
        }
        livePubQueue -> liveTicks "parquet"
        livePubQueue -> liveSpool "если запись не вышла"
        liveCollector -> liveGaps "пропуски связи"
        liveTicks -> liveCompactor "по таймеру (async)" {
            tags "async"
        }
        liveCompactor -> liveCompacted "пишет"
        liveCompacted -> liveBackup "по таймеру (async)" {
            tags "async"
        }
        liveBackup -> remoteCopy "копия (async)" "rclone" {
            tags "async"
        }
        liveBars -> liveBarsCompactor "по таймеру (async)" {
            tags "async"
        }
        liveBarsCompactor -> liveBarsCompacted "пишет"
        liveBarsCompacted -> liveBarsBackup "по таймеру (async)" {
            tags "async"
        }
        liveBarsBackup -> remoteCopy "копия баров (async)" "rclone" {
            tags "async"
        }

        # Code — Сбор цен
        okxPublic -> lcWs "котировки (async)" "WebSocket" {
            tags "async"
        }
        bybitPublic -> lcWs "котировки (async)" "WebSocket" {
            tags "async"
        }
        lcPairs -> lcWs "список подписок"
        lcWs -> lcReconnect "разрыв (async)" {
            tags "async"
        }
        lcReconnect -> lcWs "снова слушать (async)" {
            tags "async"
        }
        lcWs -> lcSpread "книги после приёма"
        lcSpread -> lcEnqueue "готовая строка"
        lcEnqueue -> livePubQueue "батч (async)" {
            tags "async"
        }
        livePubQueue -> lcWrite "worker забирает"
        lcSchema -> lcWrite "колонки"
        lcWrite -> liveTicks "parquet"
        lcWrite -> lcSpoolRec "если запись не вышла"
        lcSpoolRec -> liveSpool "файл запаса"
        lcSpoolRec -> liveTicks "повтор"
        lcReconnect -> lcGapsMod "интервал разрыва"
        lcGapsMod -> liveGaps "jsonl"
        lcWs -> lcBarsOff "свечи (флаг выключен)" {
            tags "async"
        }

        # Code — Уплотнение тиков / Копия тиков / бары
        liveTicks -> lcompMerge "читает"
        lcompMerge -> lcompWrite "слитое окно"
        lcompWrite -> liveCompacted "пишет"
        liveCompacted -> lbakPick "по таймеру (async)" {
            tags "async"
        }
        lbakPick -> lbakSend "список файлов"
        lbakSend -> remoteCopy "rclone"
        lbakSend -> lbakManifest "отметить отправленное"
        liveBars -> lbarcMerge "читает"
        lbarcSchema -> lbarcMerge "колонки бара"
        lbarcMerge -> liveBarsCompacted "пишет"
        liveBarsCompacted -> lbarbHive "по таймеру (async)" {
            tags "async"
        }
        lbarbHive -> lbarbSend "список файлов"
        lbarbSend -> remoteCopy "rclone"

        # Contour — Canary prices (PR #53 topology only)
        okxPublic -> canaryDiscovery "список инструментов" "REST"
        bybitPublic -> canaryDiscovery "список инструментов" "REST"
        canaryDiscovery -> canaryDelta "пишет снимок"
        human -> canaryDrop "список снятия"
        canaryDelta -> canaryCollector "опрос (async)" {
            tags "async"
        }
        canaryDrop -> canaryCollector "опрос (async)" {
            tags "async"
        }
        okxPublic -> canaryCollector "котировки (async)" "WebSocket" {
            tags "async"
        }
        bybitPublic -> canaryCollector "котировки (async)" "WebSocket" {
            tags "async"
        }
        canaryCollector -> canaryPubQueue "батч (async)" {
            tags "async"
        }
        canaryPubQueue -> canaryTicks "parquet"
        canaryPubQueue -> canarySpool "если запись не вышла"
        canaryCollector -> canaryGaps "пропуски связи"

        # Code — Поиск новых пар (PR #53)
        human -> cdMain "таймер / руками"
        okxPublic -> cdRest "список инструментов" "REST"
        bybitPublic -> cdRest "список инструментов" "REST"
        cdMain -> cdRest "запуск"
        cdRest -> cdIntersect "два списка"
        cdIntersect -> cdDelta "разница"
        cdDelta -> canaryDelta "атомарная запись"

        # Code — Сбор цен (изолированный) (PR #53 hot-add)
        canaryDelta -> ccHotAdd "опрос (async)" {
            tags "async"
        }
        canaryDrop -> ccHotAdd "опрос (async)" {
            tags "async"
        }
        ccHotAdd -> ccTasks "добавить или снять пару (async)" {
            tags "async"
        }
        ccPairs -> ccWs "стартовый список"
        ccTasks -> ccWs "новые слушатели (async)" {
            tags "async"
        }
        okxPublic -> ccWs "котировки (async)" "WebSocket" {
            tags "async"
        }
        bybitPublic -> ccWs "котировки (async)" "WebSocket" {
            tags "async"
        }
        ccWs -> ccSpread "книги"
        ccSpread -> ccEnqueue "готовая строка"
        ccEnqueue -> canaryPubQueue "батч (async)" {
            tags "async"
        }
        canaryPubQueue -> ccWrite "worker забирает"
        ccWrite -> canaryTicks "parquet"
        ccWrite -> ccSpoolRec "если запись не вышла"
        ccSpoolRec -> canarySpool "файл запаса"
        ccSpoolRec -> canaryTicks "повтор"
        ccWs -> ccGapsMod "разрыв"
        ccGapsMod -> canaryGaps "jsonl"

        # Contour — Stub B
        okxPublic -> stubBot "котировки (async)" "WebSocket" {
            tags "async"
        }
        bybitPublic -> stubBot "котировки (async)" "WebSocket" {
            tags "async"
        }
        stubBot -> stubJournal "намерение, без отправки"
        stubJournal -> stubBackup "по таймеру (async)" {
            tags "async"
        }
        stubBackup -> remoteCopy "копия (async)" "rclone" {
            tags "async"
        }
        okxPublic -> gear2Bot "котировки (async)" "WebSocket" {
            tags "async"
        }
        bybitPublic -> gear2Bot "котировки (async)" "WebSocket" {
            tags "async"
        }
        gear2Bot -> gear2Journal "намерение, без отправки"
        gear2Journal -> gear2Backup "по таймеру (async)" {
            tags "async"
        }
        gear2Backup -> remoteCopy "копия (async)" "rclone" {
            tags "async"
        }

        # Code — Решение без отправки
        okxPublic -> sbBooks "котировки (async)" "WebSocket" {
            tags "async"
        }
        bybitPublic -> sbBooks "котировки (async)" "WebSocket" {
            tags "async"
        }
        sbLoop -> sbBooks "старт слушателей (async)" {
            tags "async"
        }
        sbBooks -> sbDecide "тику"
        sbDecide -> sbStub "намерение"
        sbStub -> sbJournal "две ноги, без сокета"
        sbJournal -> stubJournal "jsonl"

        stubJournal -> sbbGuard "по таймеру (async)" {
            tags "async"
        }
        sbbGuard -> sbbSend "журнал, не деревья сборщика"
        sbbSend -> remoteCopy "rclone"

        # Code — Решение без отправки (4 монеты)
        okxPublic -> g2Books "котировки (async)" "WebSocket" {
            tags "async"
        }
        bybitPublic -> g2Books "котировки (async)" "WebSocket" {
            tags "async"
        }
        g2Loop -> g2Books "старт слушателей (async)" {
            tags "async"
        }
        g2Books -> g2Decide "тику"
        g2Decide -> g2Stub "намерение"
        g2Stub -> g2Journal "две ноги, без сокета"
        g2Journal -> gear2Journal "jsonl"

        gear2Journal -> g2bGuard "по таймеру (async)" {
            tags "async"
        }
        g2bGuard -> g2bSend "журнал, не деревья сборщика"
        g2bSend -> remoteCopy "rclone"

        # Contour — Canary B
        okxPublic -> thetaBot "котировки (async)" "WebSocket" {
            tags "async"
        }
        bybitPublic -> thetaBot "котировки (async)" "WebSocket" {
            tags "async"
        }
        thetaBot -> thetaMetrics "метрики (async)" {
            tags "async"
        }
        thetaBot -> thetaJournal "намерение, без отправки"

        # Code — Решение раз в секунду, без отправки
        okxPublic -> thBooks "котировки (async)" "WebSocket" {
            tags "async"
        }
        bybitPublic -> thBooks "котировки (async)" "WebSocket" {
            tags "async"
        }
        thLoop -> thBooks "старт (async)" {
            tags "async"
        }
        thBooks -> thWatch "книги раз в секунду"
        thWatch -> thFloor "снимок"
        thWatch -> thTw "снимок"
        thFloor -> thetaMetrics "jsonl"
        thTw -> thetaMetrics "jsonl"
        thWatch -> thDecide "снимок раз в секунду"
        thRules -> thDecide "пороги"
        thDecide -> thJournal "намерение, без отправки"
        thJournal -> thetaJournal "jsonl"

        # Contour — Live send (two processes, same send path, do not share one process)
        okxPublic -> walBot "котировки (async)" "WebSocket" {
            tags "async"
        }
        bybitPublic -> walBot "котировки (async)" "WebSocket" {
            tags "async"
        }
        walBot -> walQueue "постановка (async)" {
            tags "async"
        }
        walQueue -> okxOrders "отправка ордера (async)" "WebSocket" {
            tags "async"
        }
        walQueue -> bybitOrders "отправка ордера (async)" "WebSocket" {
            tags "async"
        }
        okxOrders -> walBot "ACK (async)" "WebSocket" {
            tags "async"
        }
        bybitOrders -> walBot "ACK (async)" "WebSocket" {
            tags "async"
        }
        walBot -> walJournal "журнал"

        okxPublic -> g22Bot "котировки (async)" "WebSocket" {
            tags "async"
        }
        bybitPublic -> g22Bot "котировки (async)" "WebSocket" {
            tags "async"
        }
        g22Bot -> g22Queue "постановка (async)" {
            tags "async"
        }
        g22Queue -> okxOrders "отправка ордера (async)" "WebSocket" {
            tags "async"
        }
        g22Queue -> bybitOrders "отправка ордера (async)" "WebSocket" {
            tags "async"
        }
        okxOrders -> g22Bot "ACK (async)" "WebSocket" {
            tags "async"
        }
        bybitOrders -> g22Bot "ACK (async)" "WebSocket" {
            tags "async"
        }
        g22Bot -> g22Journal "журнал"
        g22Bot -> g22Wire "провод"

        # Code — Решение и отправка (2 монеты)
        okxPublic -> walBooks "котировки (async)" "WebSocket" {
            tags "async"
        }
        bybitPublic -> walBooks "котировки (async)" "WebSocket" {
            tags "async"
        }
        walLoop -> walBooks "старт (async)" {
            tags "async"
        }
        walLoop -> walWarm "старт тёплых сокетов (async)" {
            tags "async"
        }
        walWarm -> okxOrders "держать канал (async)" "WebSocket" {
            tags "async"
        }
        walWarm -> bybitOrders "держать канал (async)" "WebSocket" {
            tags "async"
        }
        walBooks -> walDecide "тику рынка"
        walDecide -> walGate "если есть намерение"
        walGate -> walEnq "разрешено — в очередь (async)" {
            tags "async"
        }
        walEnq -> walQueue "кадр (async)" {
            tags "async"
        }
        walQueue -> walSend "забрать и послать (async)" {
            tags "async"
        }
        walSend -> okxOrders "отправка ордера (async)" "WebSocket" {
            tags "async"
        }
        walSend -> bybitOrders "отправка ордера (async)" "WebSocket" {
            tags "async"
        }
        okxOrders -> walAck "ответ (async)" "WebSocket" {
            tags "async"
        }
        bybitOrders -> walAck "ответ (async)" "WebSocket" {
            tags "async"
        }
        walAck -> walJourn "итог"
        walJourn -> walJournal "jsonl"

        # Code — Решение и отправка (30 монет)
        okxPublic -> g22Books "котировки (async)" "WebSocket" {
            tags "async"
        }
        bybitPublic -> g22Books "котировки (async)" "WebSocket" {
            tags "async"
        }
        g22Loop -> g22Books "старт (async)" {
            tags "async"
        }
        g22Loop -> g22Warm "старт тёплых сокетов (async)" {
            tags "async"
        }
        g22Warm -> okxOrders "держать канал (async)" "WebSocket" {
            tags "async"
        }
        g22Warm -> bybitOrders "держать канал (async)" "WebSocket" {
            tags "async"
        }
        g22Books -> g22Decide "снимок раз в секунду"
        g22Rules -> g22Decide "пороги"
        g22Decide -> g22Gate "если есть намерение"
        g22Gate -> g22Enq "разрешено — в очередь (async)" {
            tags "async"
        }
        g22Enq -> g22Queue "кадр (async)" {
            tags "async"
        }
        g22Queue -> g22Send "забрать и послать (async)" {
            tags "async"
        }
        g22Send -> okxOrders "отправка ордера (async)" "WebSocket" {
            tags "async"
        }
        g22Send -> bybitOrders "отправка ордера (async)" "WebSocket" {
            tags "async"
        }
        okxOrders -> g22Ack "ответ (async)" "WebSocket" {
            tags "async"
        }
        bybitOrders -> g22Ack "ответ (async)" "WebSocket" {
            tags "async"
        }
        g22Ack -> g22Journ "итог"
        g22Journ -> g22Journal "jsonl"
        g22Send -> g22WireMod "после удачной отправки"
        g22Warm -> g22WireMod "приём на проводе"
        g22WireMod -> g22Wire "jsonl"

        # Contour — Simulator (offline; does not call the live collector)
        human -> simReplay "запускает"
        simReplay -> histTicks "читает"
        simReplay -> simFeatures "признаки"
        simReplay -> simTrades "сделки прогона"

        # Code — Прогон истории
        human -> simNb "запускает ноутбук"
        human -> simRun "запускает скрипт"
        simNb -> histTicks "читает"
        simFeat -> histTicks "читает"
        simFeat -> simFeatures "пишет таблицу"
        simRun -> simFeatures "читает 1 Гц"
        simKnobs -> simRules "пороги"
        simRules -> simRun "решение на секунду"
        simRun -> simTrades "сделки прогона"
        simNb -> simRun "может вызвать тот же прогон"
    }

    views {
        systemContext arbBots "SystemContext" {
            title "System Context"
            description "Человек и внешние стороны вокруг arb_bots. Это союз возможностей, не один живой контур."
            include *
            autolayout lr
        }

        container arbBots "d-live" {
            title "Contour — Live prices"
            description "Сбор цен, очередь записи, журнал тиков, уплотнение, копия. Клик по синему процессу → Code — …"
            include liveCollector livePubQueue liveTicks liveSpool liveGaps liveCompactor liveCompacted liveBackup liveBars liveBarsCompactor liveBarsCompacted liveBarsBackup okxPublic bybitPublic remoteCopy
            autolayout tb
        }

        container arbBots "d-hotadd-canary" {
            title "Contour — Canary prices"
            description "Изолированный сбор цен и поиск новых пар. Не боевые тики. Клик по синему процессу → Code — …"
            include canaryDiscovery canaryDelta canaryDrop canaryCollector canaryPubQueue canaryTicks canarySpool canaryGaps okxPublic bybitPublic human
            autolayout tb
        }

        container arbBots "b-stub" {
            title "Contour — Stub B"
            description "Решение по своим книгам, журнал намерений, без отправки ордера. Клик по синему процессу → Code — …"
            include stubBot stubJournal stubBackup gear2Bot gear2Journal gear2Backup okxPublic bybitPublic remoteCopy
            autolayout tb
        }

        container arbBots "b-theta-would-send" {
            title "Contour — Canary B"
            description "Решение раз в секунду, журнал намерений, без отправки ордера. Клик по синему процессу → Code — …"
            include thetaBot thetaMetrics thetaJournal okxPublic bybitPublic
            autolayout lr
        }

        container arbBots "b-live-send" {
            title "Contour — Live send"
            description "Решение, очередь ордеров, отправка ордера. Клик по синему процессу → Code — … Два процесса, не смешивать часы."
            include walBot walQueue walJournal g22Bot g22Queue g22Journal g22Wire okxPublic bybitPublic okxOrders bybitOrders
            autolayout tb
        }

        container arbBots "m-sim" {
            title "Contour — Simulator"
            description "Прогон истории. Клик по «Прогон истории» → Code — Прогон истории."
            include simReplay histTicks simFeatures simTrades human
            autolayout lr
        }

        component liveCollector "code-live-collector" {
            title "Code — Сбор цен"
            description "Модули боевого сборщика. Приём и спред заморожены. Запись баров в юните выключена."
            include *
            autolayout tb
        }

        component liveCompactor "code-live-compactor" {
            title "Code — Уплотнение тиков"
            description "Oneshot app.storage.compactor"
            include *
            autolayout lr
        }

        component liveBackup "code-live-backup" {
            title "Code — Копия тиков"
            description "Oneshot app.storage.backup_transfer"
            include *
            autolayout lr
        }

        component liveBarsCompactor "code-live-bars-compactor" {
            title "Code — Уплотнение баров"
            description "Oneshot app.storage.bars_compactor. Писатель /data/bars на VPS неизвестен."
            include *
            autolayout lr
        }

        component liveBarsBackup "code-live-bars-backup" {
            title "Code — Копия баров"
            description "Тот же backup_transfer, --layout hive"
            include *
            autolayout lr
        }

        component canaryDiscovery "code-canary-discovery" {
            title "Code — Поиск новых пар"
            description "PR #53 app.discovery. Нет на main. Не смешан с боевым сбором цен."
            include *
            autolayout tb
        }

        component canaryCollector "code-canary-collector" {
            title "Code — Сбор цен (изолированный)"
            description "Тот же скрипт, другой каталог. Опрос новинок — PR #53, не REST из сборщика."
            include *
            autolayout tb
        }

        component stubBot "code-stub-bot" {
            title "Code — Решение без отправки"
            description "Публичные книги, заглушка, журнал намерений. Нет торгового канала."
            include *
            autolayout tb
        }

        component stubBackup "code-stub-backup" {
            title "Code — Копия журнала"
            description "app.bot.backup, отказ от деревьев сборщика"
            include *
            autolayout lr
        }

        component gear2Bot "code-gear2-bot" {
            title "Code — Решение без отправки (4 монеты)"
            description "Тот же вход, решение по рынку на 4 монеты."
            include *
            autolayout tb
        }

        component gear2Backup "code-gear2-backup" {
            title "Code — Копия журнала (4 монеты)"
            description "app.bot.backup, префикс spread-bbot-gear2"
            include *
            autolayout lr
        }

        component thetaBot "code-theta-bot" {
            title "Code — Решение раз в секунду, без отправки"
            description "Наблюдатели в том же процессе. Юнит не в git."
            include *
            autolayout tb
        }

        component walBot "code-wal-bot" {
            title "Code — Решение и отправка (2 монеты)"
            description "Тик рынка → очередь → ws.send. Путь W6 не рисуем: на юните выключен. Private/wire путь неизвестен."
            include *
            autolayout tb
        }

        component g22Bot "code-g22-bot" {
            title "Code — Решение и отправка (30 монет)"
            description "Часы раз в секунду, тот же путь отправки. Не смешивать процесс с 2 монетами."
            include *
            autolayout tb
        }

        component simReplay "code-sim-replay" {
            title "Code — Прогон истории"
            description "Офлайн. Не клиент живого сборщика. Fill = цена этой секунды, не задержка заглушки."
            include *
            autolayout tb
        }

        styles {
            element "Person" {
                shape Person
                background #08427b
                color #ffffff
            }
            element "Software System" {
                background #1168bd
                color #ffffff
            }
            element "External" {
                background #999999
                color #ffffff
            }
            element "Container" {
                background #438dd5
                color #ffffff
            }
            element "Component" {
                background #85bbf0
                color #000000
            }
            element "Database" {
                shape Cylinder
                background #438dd5
                color #ffffff
            }
            element "Queue" {
                shape Pipe
                background #85bbf0
                color #000000
            }
            relationship "Relationship" {
                routing Direct
            }
            relationship "async" {
                dashed true
            }
        }
    }
}
