# NOW

Снимок на 2026-11-22 ~09:55 МСК. Каждая секция — зона своего агента, чужие секции не переписывать.

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

- gear23 / B2.3 live canary, PID 821896, запущен 20.11 11:33 МСК (resume c84f73e7, без RVN: OKX делистнул RVNUSDT perp 19.11), аптайм ~47 ч на 22.11 10:45; рестартов за 21.11 нет, halts нет, ERROR/Traceback за 21.11 нет. TODO: убрать INIT из 1x resume-launcher (c84f73e7 закрыта 20.11 13:01).
- Пул 54 из 60 (2 базовые WAL, LA + 52 extras; сайдкар считает 55 вместе с RVN); последнее добавление 20.11 10:10 (YB). 21.11 и 22.11 10:10 добавление пропущено: сайдкар «bot_not_on_dyn_launcher» (22.11 would_add ENSO, AKE no_complete_meta), бот на resume-launcher. 1x: у 9 монет (WAL, LA, ACU, APR, AZTEC, BLEND, INIT, PARTI, YB) нет gear23_leverage_one_confirmed в логе, они в BBOT_CONFIRMED_1X_COINS.
- Позиций нет (heartbeat position=None на 10:41).
- completed_cycles 44 на конец 21.11 (было 36).
- 21.11: 8 сделок (IRYS, BARD×3, INIT×2, YB×2), net $+0.0954 (среднее $+0.0119); funding итого -0.192$ (OKX +0.192, Bybit -0.384), съел Bybit; funding сходится с closedPnl и private WS. insufficient_size 26, send_abort 0, обрывов private WS 16 (2 рядом со входом 092f6135).
- Мониторинг: утренний дайджест ~10:30 МСК, эта секция обновляется после дайджеста.
- size_check только по L1. REST бирж только для разовой сверки funding/PnL.

## Ops / collector (голова)

Сервер, утренний снимок, скрипты юнитов. Живые ордера и would_send не веду. Архитектура: `docs/`.

- Вектор: держать D зелёным (collector/compact/backup) как базу под would_send canary → 2.2 → 2.3 (готово) → **3** → 3.4… из [roadmap.md](roadmap.md); параллельно gear23 live (бот, динамический пул).
- Хост: uptime ~9 сут 4 ч (перезагрузка 13.11 05:15 МСК). `spread-collector-next` (PID 917, active с 05:15 МСК 13.11, рестартов нет, 203 пары, failures=0), таймеры compactor/backup/discovery/top10/ops-metrics активны. Окно топ-10 за 24ч: 287/289 слотов.
- Юнит `spread-collector-next`, код `/root/spread_staging`, данные `/data/live`. `/root/spread_staging` не git-checkout, SHA нет: крутится задеплоенное дерево. Деплоев кода и изменений unit-файлов за сутки не видно. Старый `spread-collector` inactive/disabled.
- gear23 live (код `gear23-b2.3-tw-deque-20261010-tradeobs-20261018T1741Z`, тот же): PID 821896 с 20.11 11:33 МСК (~46 ч), `completed_cycles` 45; по canary_state открыта IRYS long 10 USDT с 22.11 03:27 МСК. Зона бота. Юнит `spread-bbot.service` (старый stub) disabled.
- Backup (rclone в `backup1tb:spread-compacted`) по факту доставки: **D по backup не зелёный** — за 24ч 288 ok / 91 fail попыток (норма ~9; все файлы позже доставлены): copyto watchdog 120–196 с, кластер 21.11 09:42 – 22.11 00:00 МСК, с 00:00 МСК сбоев нет; неотправленных `*.parquet` в корне `/data/compacted` 1 (свежий, ~2 мин), последний успешный transfer 22.11 09:32 МСК.
- Ночной `spread-bbot-gear23-metrics-compact` 22.11 03:15 МСК: Result=success, days_errors=0 (compacted 4, 16.11 skip_already_compacted); `systemctl --failed` пуст.
- Топ-10 volatile 22.11 отработал 09:10 МСК: INIT IRYS ENSO BARD AKE AEON YB CP UB DGAI.
- Соседи на хосте (не веду): `spread-bbot-would-send-prod` inactive/disabled; θ K=1 canary inactive/disabled, oneshot не запускал; gear23 live работает. HL v2 не запущен.
- Ops 24ч: CPU ср. 6% / пик 24%, load сейчас ~0.3, RAM ср. 3.0 / пик 4.1 из 16 GiB (занято), egress ~7.0 GiB. Диск 40/79G (53%), свободно ~36G. Данные материализуются в `/data/live`, затем compact в `/data/compacted`, копия — `backup1tb:spread-compacted`.
- Слежу я (голова), утренний дайджест ~9:32 МСК. @бот — Contour B / gear23 live, @стратег — would_send. Sentry ops отдельно, не эта секция.
- Не поднимать старый collector. Чужие data root не чищу без явного OK. Утренний ops-снимок NOW.md коммичу сразу в main. Прочий патч в NOW.md и на сервер — только после явного «да».
