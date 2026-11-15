# NOW

Снимок на 2026-11-15 ~09:45 МСК. Каждая секция — зона своего агента, чужие секции не переписывать.

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

- gear23 / B2.3 live canary, PID 69299, запущен 13.11 18:58 МСК, аптайм ~40 ч на 15.11 10:40; рестартов и halts нет, ERROR/Traceback за 14.11 нет (private WS: 21 обрыв, 11 неудачных auth, все с переподключением).
- Пул 52 из 60 (3 базовые WAL, RVN, LA + 49 extras); 14.11 сайдкар добавил BERA, SHELL (1x подтверждён); 15.11 10:10 МСК ротация без добавлений, весь топ-10 уже в пуле. Нет подтверждения 1x в логе у APR и BLEND.
- Позиций нет (flat, heartbeat 15.11 10:37 МСК).
- completed_cycles 8 на конец 14.11 (у PID 69299).
- 14.11: 1 сделка, INIT long `731e7da2` (23:45→00:28 МСК), net $+0.0004; funding OKX 0, Bybit -$0.0149, сходится с closedPnl Bybit. insufficient_size 10 (INIT 9, BTW 1).
- Мониторинг: утренний дайджест ~10:30 МСК, эта секция обновляется после дайджеста.
- size_check только по L1. REST бирж только для разовой сверки funding/PnL.

## Ops / collector (голова)

Сервер, утренний снимок, скрипты юнитов. Живые ордера и would_send не веду. Архитектура: `docs/`.

- Вектор: держать D зелёным (collector/compact/backup) как базу под would_send canary → 2.2 → 2.3 (готово) → **3** → 3.4… из [roadmap.md](roadmap.md); параллельно gear23 live (бот, динамический пул).
- Хост: uptime ~2 сут 4 ч (перезагрузка 13.11 05:15 МСК, kernel 5.15.0-203). `spread-collector-next` (PID 917, active с 05:15 МСК 13.11, рестартов нет), таймеры compactor/backup/discovery/top10/ops-metrics активны. Окно топ-10 за 24ч: 287/289 слотов.
- Юнит `spread-collector-next`, код `/root/spread_staging`, данные `/data/live`. `/root/spread_staging` не git-checkout, SHA нет: крутится задеплоенное дерево. Деплоев кода и изменений unit-файлов за сутки не видно. Старый `spread-collector` inactive/disabled.
- gear23 live (процесс `/gear23/`, код `gear23-b2.3-tw-deque-20261010-tradeobs-20261018T1741Z`) работает: PID 69299 с 13.11 18:58 МСК (~38 ч), `completed_cycles` 8; по canary_state открыта позиция INIT. Запускал не я, это зона бота. Рядом крутится enabled-юнит `spread-bbot.service` (старый stub, PID 920, `BBOT_PROFILE=signal_test`, `BBOT_COINS=BTC,ETH,LA,DOGE`, `/data/bbot`) — это не gear23.
- Backup (rclone в `backup1tb:spread-compacted`) по факту доставки: D зелёный — за 24ч 287 ok / 0 fail (норма ~9), неотправленных `*.parquet` в корне `/data/compacted` 1 (~3 мин), последний успешный transfer 15.11 09:36 МСК.
- Compactor по таймеру без ошибок. Топ-10 volatile 15.11 отработал 09:10–09:11 МСК: BTW AKE SOPH INIT BERA BARD AEON CP AXIS SHELL.
- `systemctl --failed` пуст. Старые one-shot/canary юниты (`spread-bars-compactor`, `spread-bars-compacted-backup-transfer`, `spread-bbot-ev2-10/11/12`, `gear23-shadow-end-one-hour-20261024t1219`) inactive. Не чистил и не сбрасывал.
- Соседи на хосте (не веду): `spread-bbot-would-send-prod` inactive/disabled; θ K=1 canary inactive/disabled, oneshot не запускал; gear23 live работает (см. выше). HL v2 не запущен.
- Ops 24ч: CPU ср. 16% / пик 40%, load сейчас ~0.4, RAM ср. 1.8 / пик 2.9 из 16 GiB, egress ~9.2 GiB. Лимиты collector: skew/age 2000 мс, HOT_ADD max_extra=8, bars off. Диск 40/79G (52%), свободно ~37G. Данные материализуются в `/data/live`, затем compact в `/data/compacted`, копия — `backup1tb:spread-compacted`.
- Слежу я (голова), утренний дайджест ~9:32 МСК. @бот — Contour B / gear23 live, @стратег — would_send. Sentry ops отдельно, не эта секция.
- Не поднимать старый collector. Чужие data root не чищу без явного OK. Утренний ops-снимок NOW.md коммичу сразу в main. Прочий патч в NOW.md и на сервер — только после явного «да».
