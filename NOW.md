# NOW

Снимок на 2026-10-03. Каждая секция — зона своего агента, чужие секции не переписывать.

## would_send (стратег)

Симуляция гира 2.2, `would_send`, `send=false`. Живых ордеров нет.

- Юнит `spread-bbot-would-send-prod`, данные `/data/bbot-would-send-prod`.
- Код на VPS `a12d593`. В main уже hot-add (#64, `426d689`) и деплой-доки (#68, `3cd1845`). Работающий контур с этого tip main не перекатывался.
- Ротация expand-only каждый день в 10:00 МСК по утреннему std-spread. Дропов нет.
- Пул после ротации 03.10: 42 = база 29 + 13 экстра. Экстра: CT, AEON, OPN, ARX, RECALL, LQTY, SAND, MANA, ENJ, RESOLV, SENT, WOO, TRUTH. Утром добавлены SAND, MANA, ENJ, RESOLV, SENT, WOO, TRUTH. 2Z был в топ-10, но уже в базе.
- Открыт SAND long с 03.10 12:59:55 МСК, `d0c91dee`, θ_1m≈0.507, spread_IN +0.678, floor +0.169, p50_1m +0.676. Рестарт со сбросом этой сделки предложен и не сделан.
- Шов: рестарт прогревает hot-add экстра из устаревшей history, floor прыгает. Не починено.
- Гейты: θ_open 0.50, p50_open 0.60, min_profit 0.20, fee 0.30, K=1.
- Канарейка theta-k1 остановлена. Contour B этот юнит не трогает.
- Следит Sentry (issue created) на открытиях.

## Ops / collector (голова)

Сервер, утренний снимок, скрипты юнитов. Живые ордера и would_send не веду.

- Prod writer один: `spread-collector-next` → `/data/live`. Старый collector не поднимать.
- Утро ~9:32 МСК: диск, техпроцессы, топ-10 volatile (OKX–Bybit, rolling 24ч).
- Compact/backup прода и скрипты юнитов — эта зона. Чужие data root не чищу без явного OK.
- HL v2 (пул, `/data/live_hl_v2`, бэкап `spread-hl-v2`) ведёт Старшой, не эта секция.
- Эту секцию по утрам обновляю только после мержа в main. Патч на сервер сюда не пишу без явного «да».
