# NOW

Снимок на 2026-11-13 ~09:45 МСК. Каждая секция — зона своего агента, чужие секции не переписывать.

Вектор развития: [roadmap.md](roadmap.md). Этот файл — снимок, не план.

## would_send (стратег)

Симуляция гира 2.2, `would_send`, `send=false`. Живых ордеров нет. **Сейчас остановлена.**

- Юнит `spread-bbot-would-send-prod` — inactive/disabled. Данные `/data/bbot-would-send-prod` (`theta/`, `tw_p50/`, `theta_trades/`, `floor/`, 5.8G) сохранены, не чистить.
- Checkout на хосте `/root/spread_bbot_would_send_prod` = `8f942d2` **+ незакоммиченные правки Codex A/B** (10 файлов в `git status`). Прод ≠ main. Не делать `git checkout`/`reset` на хосте.
- Статус: остановлен и выключен по команде Миши 22.10.2026 14:43 МСК во flat (последняя сделка IOST long `fada8207` закрыта 22.10 07:38 МСК). Последний пул 77 (база 29 + 48 экстра, потолок `BBOT_HOT_ADD_MAX_EXTRA=48`).
- Rotate-таймер `spread-bbot-would-send-prod-rotate` выключен (inactive/disabled) с 31.10.2026 11:47 МСК по команде Миши. За время простоя он дописал экстра в `driver/rotate_state.json` (56 > потолка 48) — решить перед запуском.
- Гейты без изменений: θ_open 0.50, p50_open 0.60, min_profit 0.20, K=1, notional 20 USDT, AGE/SKEW 10 s, `BBOT_SIZE_GATE=0`, `send=false`.
- Открытый вопрос: у OKX `ct_val` = null для новых монет (MEW/IOST/DOOD) — плановый размер vs глубина L1 в неверных единицах; починить до любого запуска.
- Запуск: `systemctl enable --now spread-bbot-would-send-prod spread-bbot-would-send-prod-rotate.timer` только по команде Миши; экстра-монеты прогреваются из устаревшей истории на старте.
- Следит: никто — Sentry- и утренняя pool-рутины стратега на паузе.
- Не трогать Contour B, `/data/live`, collector, чужие юниты, данные и checkout would_send.

## Contour B (бот)

- gear23 / B2.3 live canary, PID 21096, запущен 13.11 09:53 МСК (после ребута VPS 04:53–05:15; патч старых баров floor_watcher, cycles с нуля), рестартов и halts после запуска нет, ERROR/Traceback за 12.11 нет.
- Пул 50 из 60 (3 базовые WAL, RVN, LA + 47 extras); 13.11 10:10 МСК сайдкар добавил BARD, IMX (1x подтверждён, warm history_missing, гейт floor_warm), остальное из топ-10 уже в пуле. Без подтверждения 1x в журнале с рестарта: ACU, APR, AZTEC, BLEND, PARTI.
- Открыта INIT long `c381d289` (с 13.11 10:04 МСК, 10 USD ноционал).
- completed_cycles 55 (до рестарта), у нового PID с нуля.
- 12.11: 2 сделки (BTW), net $-0.0815 (в среднем $-0.0408), funding OKX +0.1234 / Bybit -0.1874; Bybit сверен с private WS и closedPnl, сходится; ERROR/Traceback 0, обрывов private WS 9, рядом со сделками нет.
- Мониторинг: утренний дайджест ~10:30 МСК, эта секция обновляется после дайджеста.
- size_check только по L1. REST бирж только для разовой сверки funding/PnL.

## Ops / collector (голова)

Сервер, утренний снимок, скрипты юнитов. Живые ордера и would_send не веду. Архитектура: `docs/`.

- Вектор: держать D зелёным (collector/compact/backup) как базу под would_send canary → 2.2 → 2.3 (готово) → **3** → 3.4… из [roadmap.md](roadmap.md); параллельно gear23 live (бот, динамический пул).
- **Хост перезагружен:** poweroff 13.11 04:53 МСК (01:53 UTC), загрузка 05:15 МСК (kernel 5.15.0-203, был -179), простой ~22 мин, uptime ~4.4 ч. Причину (провайдер или плановое) не знаю. После загрузки сами поднялись enabled-юниты: `spread-collector-next` (PID 917, active с 05:15 МСК, heartbeat 203 пары, `failures=0`, рестартов 0), таймеры compactor/backup/discovery/top10/ops-metrics. Окно топ-10 за 24ч: 280/289 слотов (дыра от простоя).
- Юнит `spread-collector-next`, код `/root/spread_staging`, данные `/data/live`. `/root/spread_staging` не git-checkout, SHA нет: крутится задеплоенное дерево. Деплоев кода за сутки нет; изменений unit-файлов за сутки нет. Старый `spread-collector` inactive/disabled.
- **gear23 live (процесс `/gear23/`) после ребута не запущен:** 13.11 01:53 UTC остановлен по SIGHUP при выключении (`done_reason=operator_signal_sighup`, позиция пуста — был flat), автозапуска нет. Вместо него с 05:15 МСК крутится enabled-юнит `spread-bbot.service` (старый stub, PID 920, `BBOT_PROFILE=signal_test`, `BBOT_COINS=BTC,ETH,LA,DOGE`, `/data/bbot`, без ключей в unit) — это не gear23. Запуск gear23 — зона и решение бота/Миши, я не трогал.
- Backup (rclone в `backup1tb:spread-compacted`) по факту доставки: **D зелёный** — за 24ч 282 ok / 2 fail (норма ~9; оба `copyto exceeded watchdog timeout 120s`, один файл `…T193500Z_T194000Z` 12.11 22:46 и 22:51 МСК, потом доставлен), неотправленных в `/data/compacted` 0, последний успешный transfer 13.11 09:38 МСК.
- Compactor по таймеру без ошибок. Ночной compact 03:15 МСК 13.11 (`gear23-metrics-compact`, 4 дня сжато, days_errors=0, exit 0) отработал до перезагрузки; θ K=1 compact штатно. Топ-10 volatile 13.11 отработал 09:10–09:11 МСК.
- `systemctl --failed` пуст. Старые one-shot/canary юниты (`spread-bars-compactor`, `spread-bars-compacted-backup-transfer` с 13.08; `spread-bbot-ev2-10/11/12` с 21–23.09; `gear23-shadow-end-one-hour-20261024t1219` с 24.10) inactive. Не чистил и не сбрасывал.
- Соседи на хосте (не веду): `spread-bbot-would-send-prod` inactive/disabled; θ K=1 canary inactive/disabled, oneshot не запускал; gear23 live не запущен (см. выше). HL v2 не запущен.
- Ops 24ч: CPU ср. 7% / пик 26%, load сейчас ~0.3, RAM пик 2.4 из 16 GiB (свободно ≥14 GiB), egress ~7.3 GiB. Лимиты collector: skew/age 2000 мс, HOT_ADD max_extra=8, bars off. Диск 38/79G (50%), свободно ~39G. Данные материализуются в `/data/live`, затем compact в `/data/compacted`, копия — `backup1tb:spread-compacted`.
- Слежу я (голова), утренний дайджест ~9:32 МСК. @бот — Contour B / gear23 live, @стратег — would_send. Sentry ops отдельно, не эта секция.
- Не поднимать старый collector. Чужие data root не чищу без явного OK. Утренний ops-снимок NOW.md коммичу сразу в main. Прочий патч в NOW.md и на сервер — только после явного «да».
