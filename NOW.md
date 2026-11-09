# NOW

Снимок на 2026-11-09 ~09:40 МСК. Каждая секция — зона своего агента, чужие секции не переписывать.

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

- gear23 / B2.3 live canary, PID 1614324, работает с 25.10 13:58 МСК (~14.9 сут), рестартов и halts нет, новых ERROR/Traceback нет.
- Пул 47 из 60 (3 базовые WAL, RVN, LA + 44 extras); 09.11 10:10 МСК добавлен AZTEC (warm ok, подтверждения 1x в журнале пока нет), остальные 9 из топ-10 уже в пуле; ранее 05.11 HUMA, EWT, ARX, 06.11 XTZ, SPACE. XTZ: 1x подтверждён, warm не прошёл (history_missing).
- Позиций нет (BLEND long `a8fcc753` закрыта 08.11 17:00 МСК).
- completed_cycles 36.
- 08.11: 1 сделка (BLEND long), net $+0.0097; Bybit funding сверен с private WS и биржевым closedPnl, сходится; ERROR/Traceback за сутки 0.
- Мониторинг: утренний дайджест ~10:30 МСК, эта секция обновляется после дайджеста.
- size_check только по L1. REST бирж только для разовой сверки funding/PnL.

## Ops / collector (голова)

Сервер, утренний снимок, скрипты юнитов. Живые ордера и would_send не веду. Архитектура: `docs/`.

- Вектор: держать D зелёным (collector/compact/backup) как базу под would_send canary → 2.2 → **2.3** → 2.4… из [roadmap.md](roadmap.md); параллельно крутится gear23 live (бот, динамический пул).
- Юнит `spread-collector-next`, код `/root/spread_staging`, данные `/data/live`. SHA нет: `/root/spread_staging` не git-checkout, крутится задеплоенное дерево (innovation-фикс discovery = main `aae6695`, backup_transfer = main `0cb24c1`). Старый `spread-collector` inactive/disabled.
- Изменения на хосте за сутки: деплоев и рестартов юнитов нет, collector тот же PID 4176949, gear23 тот же PID 1614324. На main за сутки только docs: `f7fcb1b` (ops-снимок 08.11) и `8aede53` (секция Contour B после дайджеста бота).
- Статус 09.11 ~09:40 МСК: active с 22.09 14:07 МСК (PID 4176949, heartbeat 203 пары, `failures=0` по последнему снимку, RSS ~720 MiB).
- Backup (rclone в `backup1tb:spread-compacted`) по факту доставки: **D не зелёный по fail** — за 24ч 288 ok / 38 fail (норма ~9), все `copyto exceeded watchdog timeout 120s`, один кластер 08.11 21:27–23:06 МСК, после него 0 fail (с 00:00 МСК 09.11 — 0). Неотправленных в `/data/compacted` 1 (~2 мин), backlog 0, последний успешный transfer 09.11 09:32 МСК.
- Compactor по таймеру без ошибок. Ночной compact 03:15 МСК: таймеры gear23/θ K=1 отработали 00:15 UTC. Топ-10 volatile 09.11 отработал 09:10–09:11 МСК, окно 287/289 слотов.
- В `systemctl --failed` сейчас пусто; сами старые one-shot/canary юниты (`spread-bars-compactor`, `spread-bars-compacted-backup-transfer` с 13.08; `spread-bbot-ev2-10/11/12` с 21–23.09; `gear23-shadow-end-one-hour-20261024t1219` с 24.10) inactive. Не чистил и не сбрасывал.
- Соседи на хосте (не веду): `spread-bbot-would-send-prod` inactive/disabled; gear23 live PID 1614324 standalone с 25.10 13:58 МСК; θ K=1 canary inactive/disabled, oneshot не запускал. HL v2 не запущен.
- Ops 24h: CPU ср. 6% / пик 31%, load сейчас ~0.4, RAM пик 3.4 из 16 GiB (свободно ≥13 GiB), egress 6.1 GiB.
- Лимиты collector: skew/age 2000 мс, HOT_ADD max_extra=8, bars off. Диск 43/79G (56%), свободно ~34G. Данные впервые материализуются в `/data/live`, затем compact в `/data/compacted`, резервная копия — `backup1tb:spread-compacted`.
- Слежу я (голова), утренний дайджест ~9:32 МСК. @бот — Contour B / gear23 live, @стратег — would_send. Sentry ops отдельно, не эта секция.
- Не поднимать старый collector. Чужие data root не чищу без явного OK. Утренний ops-снимок NOW.md коммичу сразу в main. Прочий патч в NOW.md и на сервер — только после явного «да».
