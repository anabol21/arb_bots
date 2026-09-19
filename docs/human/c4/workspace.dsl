# Lite 2025.11.08 rejects containerDb inside group. Stores stay in groups as container + tags "Database" (cylinder style below). Do not restore containerDb.
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
                liveCollector = container "Сбор цен" "spread-collector.service · app/screaner_b_o.py" "Python"
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
                liveCompactor = container "Уплотнение тиков" "spread-compactor.timer · app.storage.compactor" "Python oneshot"
                liveCompacted = container "Уплотнённые тики" "/data/compacted" "parquet" {
                    tags "Database"
                }
                liveBackup = container "Копия тиков" "spread-backup-transfer.timer · app.storage.backup_transfer" "Python oneshot"
                liveBars = container "Бары (писатель неизвестен)" "/data/bars; боевой сборщик бары не пишет" "parquet" {
                    tags "Database"
                }
                liveBarsCompactor = container "Уплотнение баров" "spread-bars-compactor.timer · app.storage.bars_compactor" "Python oneshot"
                liveBarsCompacted = container "Уплотнённые бары" "/data/bars_compacted_v2" "parquet" {
                    tags "Database"
                }
                liveBarsBackup = container "Копия баров" "таймеры копии баров · app.storage.backup_transfer" "Python oneshot"
            }

            group "Contour — Canary prices" {
                canaryDiscovery = container "Поиск новых пар" "spread-discovery-hotadd-canary.timer · python -m app.discovery" "Python oneshot"
                canaryDelta = container "Список новинок" "delta-файл; не боевой список пар" "csv" {
                    tags "Database"
                }
                canaryDrop = container "Список снятия" "drop-файл" "csv" {
                    tags "Database"
                }
                canaryCollector = container "Сбор цен (изолированный)" "spread-collector-hotadd-canary.service · app/screaner_b_o.py, другой каталог" "Python"
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
                stubBot = container "Решение без отправки" "spread-bbot.service · python -m app.bot" "Python"
                stubJournal = container "Журнал намерений" "/data/bbot" "jsonl" {
                    tags "Database"
                }
                stubBackup = container "Копия журнала" "spread-bbot-backup-transfer.timer · app.bot.backup" "Python oneshot"
                gear2Bot = container "Решение без отправки (4 монеты)" "spread-bbot-gear2.service · python -m app.bot" "Python"
                gear2Journal = container "Журнал намерений (4 монеты)" "/data/bbot-gear2" "jsonl" {
                    tags "Database"
                }
                gear2Backup = container "Копия журнала (4 монеты)" "spread-bbot-gear2-backup-transfer.timer · app.bot.backup" "Python oneshot"
            }

            group "Contour — Canary B" {
                thetaBot = container "Решение раз в секунду, без отправки" "VPS unit spread-bbot-theta-k1-canary · python -m app.bot · файла юнита нет в git" "Python"
                thetaMetrics = container "Метрики наблюдения" "floor / tw_p50 / theta jsonl" "jsonl" {
                    tags "Database"
                }
                thetaJournal = container "Журнал намерений (1 Гц)" "theta_trades jsonl" "jsonl" {
                    tags "Database"
                }
            }

            group "Contour — Live send" {
                walBot = container "Решение и отправка (2 монеты)" "spread-bbot-canary-wal-eden.service · python -m app.bot" "Python"
                walQueue = container "Очередь ордеров (2 монеты)" "asyncio.Queue внутри процесса" "asyncio.Queue" {
                    tags "Queue"
                }
                walJournal = container "Журнал ордеров (2 монеты)" "/data/bbot-canary-wal-eden" "jsonl" {
                    tags "Database"
                }
                g22Bot = container "Решение и отправка (30 монет)" "spread-bbot-gear22-live-canary.service · python -m app.bot" "Python"
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
                simReplay = container "Прогон истории" "model.ipynb / research/gear22_backtest/replay.py · без systemd" "Python offline"
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

        # Contour — Simulator (offline; does not call the live collector)
        human -> simReplay "запускает"
        simReplay -> histTicks "читает"
        simReplay -> simFeatures "признаки"
        simReplay -> simTrades "сделки прогона"
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
            description "Сбор цен, очередь записи, журнал тиков, уплотнение, копия"
            include liveCollector livePubQueue liveTicks liveSpool liveGaps liveCompactor liveCompacted liveBackup liveBars liveBarsCompactor liveBarsCompacted liveBarsBackup okxPublic bybitPublic remoteCopy
            autolayout tb
        }

        container arbBots "d-hotadd-canary" {
            title "Contour — Canary prices"
            description "Изолированный сбор цен и поиск новых пар. Не боевые тики."
            include canaryDiscovery canaryDelta canaryDrop canaryCollector canaryPubQueue canaryTicks canarySpool canaryGaps okxPublic bybitPublic human
            autolayout tb
        }

        container arbBots "b-stub" {
            title "Contour — Stub B"
            description "Решение по своим книгам, журнал намерений, без отправки ордера"
            include stubBot stubJournal stubBackup gear2Bot gear2Journal gear2Backup okxPublic bybitPublic remoteCopy
            autolayout tb
        }

        container arbBots "b-theta-would-send" {
            title "Contour — Canary B"
            description "Решение раз в секунду, журнал намерений, без отправки ордера"
            include thetaBot thetaMetrics thetaJournal okxPublic bybitPublic
            autolayout lr
        }

        container arbBots "b-live-send" {
            title "Contour — Live send"
            description "Решение, очередь ордеров, отправка ордера. Два процесса, не смешивать часы решения."
            include walBot walQueue walJournal g22Bot g22Queue g22Journal g22Wire okxPublic bybitPublic okxOrders bybitOrders
            autolayout tb
        }

        container arbBots "m-sim" {
            title "Contour — Simulator"
            description "Прогон истории. Нет systemd и нет отправки ордера."
            include simReplay histTicks simFeatures simTrades human
            autolayout lr
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
