# NOW

Снимок на 2026-11-14 ~09:45 МСК. Каждая секция — зона своего агента, чужие секции не переписывать.

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

- gear23 / B2.3 live canary, PID 69299, запущен 13.11 18:58 МСК (рестарт с собственной историей floor), аптайм ~16 ч; halts нет, ERROR/Traceback за 13.11 нет; 13.11 было 2 запланированных рестарта (09:53 и 18:58), генератор дайджеста насчитал 6, расхождение не разбирал.
- Пул 52 из 60 (3 базовые WAL, RVN, LA + 49 extras); 14.11 10:10 МСК сайдкар добавил BERA, SHELL (1x подтверждён, warm history_missing, гейт floor_warm, торговать пока нельзя); AKE из топ-10 не добавлен, причина в журнале не указана. Без подтверждения 1x с рестарта 18:58: ACU, APR, AZTEC, BLEND, IRYS, PARTI.
- Открыта BTW long `835ddbe0` (с 14.11 09:17 МСК, 10 USD ноционал).
- completed_cycles 2 у PID 69299 (до рестарта было 55).
- 13.11: 11 сделок (BTW 1, INIT 10), net $+0.2918 (в среднем $+0.0265); гросс $+0.927, после комиссий $+0.591; funding OKX +0.182 / Bybit -0.481; Bybit сверен с private WS и closedPnl, сходится; обрывов private WS 11, рядом со сделками нет; insufficient_size 10 (INIT).
- Мониторинг: утренний дайджест ~10:30 МСК, эта секция обновляется после дайджеста.
- size_check только по L1. REST бирж только для разовой сверки funding/PnL.

## Ops / collector (голова)

Сервер, утренний снимок, скрипты юнитов. Живые ордера и would_send не веду. Архитектура: `docs/`.

- Вектор: держать D зелёным (collector/compact/backup) как базу под would_send canary → 2.2 → 2.3 (готово) → **3** → 3.4… из [roadmap.md](roadmap.md); параллельно gear23 live (бот, динамический пул).
- **Хост перезагружен 13.11:** poweroff 04:53 МСК, загрузка 05:15 МСК (kernel 5.15.0-203, был -179), простой ~22 мин, uptime ~28 ч. Причину (провайдер или плановое) не знаю. После загрузки сами поднялись enabled-юниты: `spread-collector-next` (PID 917, active с 05:15 МСК 13.11, heartbeat 203 пары, `failures=0`, рестартов 0), таймеры compactor/backup/discovery/top10/ops-metrics. Окно топ-10 за 24ч: 287/289 слотов.
- Юнит `spread-collector-next`, код `/root/spread_staging`, данные `/data/live`. `/root/spread_staging` не git-checkout, SHA нет: крутится задеплоенное дерево. Деплоев кода за сутки нет; изменений unit-файлов за сутки нет. Старый `spread-collector` inactive/disabled.
- gear23 live (процесс `/gear23/`, код `gear23-b2.3-tw-deque-20261010-tradeobs-20261018T1741Z`) работает: PID 69299 с 13.11 18:58 МСК (~14.7 ч), `completed_cycles` 7; открыта BTW long `835ddbe0` с 14.11 09:17 МСК. Запускал не я, это зона бота. Рядом крутится enabled-юнит `spread-bbot.service` (старый stub, PID 920, `BBOT_PROFILE=signal_test`, `BBOT_COINS=BTC,ETH,LA,DOGE`, `/data/bbot`) — это не gear23.
- Backup (rclone в `backup1tb:spread-compacted`) по факту доставки: **D не зелёный** — за 24ч 289 ok / 32 fail (норма ~9; все `copyto exceeded watchdog timeout 120s`, кластер 13.11 22:05–23:09 МСК на файлах `…T185500Z`–`…T193500Z`, потом доставлены; с 00:00 МСК 14.11 проверять отдельно), неотправленных в `/data/compacted` 0, последний успешный transfer 14.11 09:37 МСК.
- Compactor по таймеру без ошибок. Ночной compact 03:15 МСК 14.11 (`gear23-metrics-compact`, 4 дня сжато, days_errors=0, exit 0), θ K=1 compact штатно. Топ-10 volatile 14.11 отработал 09:10–09:11 МСК.
- `systemctl --failed` пуст. Старые one-shot/canary юниты (`spread-bars-compactor`, `spread-bars-compacted-backup-transfer` с 13.08; `spread-bbot-ev2-10/11/12` с 21–23.09; `gear23-shadow-end-one-hour-20261024t1219` с 24.10) inactive. Не чистил и не сбрасывал.
- Соседи на хосте (не веду): `spread-bbot-would-send-prod` inactive/disabled; θ K=1 canary inactive/disabled, oneshot не запускал; gear23 live работает (см. выше). HL v2 не запущен.
- Ops 24ч: CPU ср. 7.5% / пик 26%, load сейчас ~0.3, RAM пик 2.4 из 16 GiB (свободно ≥14 GiB), egress ~9.0 GiB. Лимиты collector: skew/age 2000 мс, HOT_ADD max_extra=8, bars off. Диск 38/79G (50%), свободно ~38G. Данные материализуются в `/data/live`, затем compact в `/data/compacted`, копия — `backup1tb:spread-compacted`.
- Слежу я (голова), утренний дайджест ~9:32 МСК. @бот — Contour B / gear23 live, @стратег — would_send. Sentry ops отдельно, не эта секция.
- Не поднимать старый collector. Чужие data root не чищу без явного OK. Утренний ops-снимок NOW.md коммичу сразу в main. Прочий патч в NOW.md и на сервер — только после явного «да».
