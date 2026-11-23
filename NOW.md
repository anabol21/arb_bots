# NOW

Снимок на 2026-11-23 ~09:45 МСК. Каждая секция — зона своего агента, чужие секции не переписывать.

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

- gear23 / B2.3 live canary, PID 821896, запущен 20.11 11:33 МСК (resume c84f73e7, без RVN: OKX делистнул RVNUSDT perp 19.11), аптайм ~71 ч на 23.11 10:40; рестартов за 22.11 нет, halts нет, ERROR/Traceback за 22.11 нет. TODO: убрать INIT из 1x resume-launcher (c84f73e7 закрыта 20.11 13:01).
- Пул 54 из 60 (2 базовые WAL, LA + 52 extras; сайдкар считает 55 вместе с RVN); последнее добавление 20.11 10:10 (YB). 21.11, 22.11 и 23.11 10:10 добавление пропущено: сайдкар «bot_not_on_dyn_launcher» (23.11 would_add ENSO), бот на resume-launcher. 1x: у 9 монет (WAL, LA, ACU, APR, AZTEC, BLEND, INIT, PARTI, YB) нет gear23_leverage_one_confirmed в логе, они в BBOT_CONFIRMED_1X_COINS.
- Позиций нет (heartbeat position=None на 10:37).
- completed_cycles 50 на конец 22.11 (было 44).
- 22.11: 6 сделок (INIT×4, IRYS, CP), net $-0.3271 (среднее $-0.0545); funding итого -0.461$ (OKX +0.110, Bybit -0.572), съел Bybit (IRYS, INIT 92eca547); funding сходится с closedPnl Bybit и private WS. insufficient_size 24, send_abort 0, обрывов private WS 4 (рядом со сделками нет).
- Мониторинг: утренний дайджест ~10:30 МСК, эта секция обновляется после дайджеста.
- size_check только по L1. REST бирж только для разовой сверки funding/PnL.

## Ops / collector (голова)

Сервер, утренний снимок, скрипты юнитов. Живые ордера и would_send не веду. Архитектура: `docs/`.

- Вектор: держать D зелёным (collector/compact/backup) как базу под would_send canary → 2.2 → 2.3 (готово) → **3** → 3.4… из [roadmap.md](roadmap.md); параллельно gear23 live (бот, динамический пул).
- Хост: uptime ~10 сут 4 ч (перезагрузка 13.11 05:15 МСК). `spread-collector-next` (PID 917, active с 05:15 МСК 13.11, рестартов нет, 204 пары в heartbeat, failures=0), таймеры compactor/backup/discovery/top10/ops-metrics активны. Окно топ-10 за 24ч: 285/289 слотов.
- Юнит `spread-collector-next`, код `/root/spread_staging`, данные `/data/live`. `/root/spread_staging` не git-checkout, SHA нет: крутится задеплоенное дерево. Деплоев кода и изменений unit-файлов за сутки не видно. Старый `spread-collector` inactive/disabled.
- gear23 live (код `gear23-b2.3-tw-deque-20261010-tradeobs-20261018T1741Z`, тот же): PID 821896 с 20.11 11:33 МСК (~71 ч), `completed_cycles` 53; по canary_state позиции нет. Зона бота. Юнит `spread-bbot.service` (старый stub) disabled.
- Backup (rclone в `backup1tb:spread-compacted`) по факту доставки: **D по backup не зелёный** — за 24ч 286 ok / 71 fail попыток (23 уникальных файла, норма ~9; все позже доставлены): copyto watchdog 128–196 с, кластер 22.11 17:06 – 23:55 МСК (пик 20–23 ч), с 00:00 МСК сбоев нет; неотправленных `*.parquet` в корне `/data/compacted` 1 (свежий, меньше минуты), последний успешный transfer 23.11 09:35 МСК.
- Ночной `spread-bbot-gear23-metrics-compact` 23.11 03:15 МСК: Result=success, days_errors=0 (compacted 4, theta_trades 21.11 rows=381 bad=0); θ K=1 compact: days_errors=0; `systemctl --failed` пуст.
- Топ-10 volatile 23.11 отработал 09:10 МСК: INIT CP ENSO BREV IRYS AEON DGAI DNA BARD EWT.
- Соседи на хосте (не веду): `spread-bbot-would-send-prod` inactive/disabled; θ K=1 canary inactive/disabled, oneshot не запускал; gear23 live работает. HL v2 не запущен.
- Ops 24ч: CPU ср. 5.5% / пик 23%, load сейчас ~0.4, RAM ср. 3.1 / пик 3.9 из 16 GiB (занято), egress ~6.1 GiB tx. Диск 40/79G (53%), свободно ~36G. Данные материализуются в `/data/live`, затем compact в `/data/compacted`, копия — `backup1tb:spread-compacted`.
- Слежу я (голова), утренний дайджест ~9:32 МСК. @бот — Contour B / gear23 live, @стратег — would_send. Sentry ops отдельно, не эта секция.
- Не поднимать старый collector. Чужие data root не чищу без явного OK. Утренний ops-снимок NOW.md коммичу сразу в main. Прочий патч в NOW.md и на сервер — только после явного «да».
