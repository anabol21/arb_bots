# NOW

Снимок на 2026-11-12 ~09:45 МСК. Каждая секция — зона своего агента, чужие секции не переписывать.

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

- gear23 / B2.3 live canary, PID 1614324, работает с 25.10 13:58 МСК (~16.9 сут), рестартов и halts нет, новых ERROR/Traceback нет.
- Пул 48 из 60 (3 базовые WAL, RVN, LA + 45 extras); 11.11 10:10 МСК сайдкар добавил UB (1x подтверждён, но прогрев не удался: history_missing, торговать пока не может, floor_warm); остальные монеты топ-10 уже были в пуле. Последнее до этого: 09.11 AZTEC. Без подтверждения 1x в журнале: AZTEC.
- Открыта INIT long `60c4c772` (с 11.11 ~10:20 МСК, 10 USD ноционал).
- completed_cycles 38.
- 10.11: 1 сделка (INIT long `c0080e92`, 20:01→23:49, hold 27ч48м), net $-2.6634 (funding Bybit -3.4601 съел плюс, OKX +0.8109); Bybit funding сверен с private WS и closedPnl, сходится; ERROR/Traceback за сутки 0.
- Мониторинг: утренний дайджест ~10:30 МСК, эта секция обновляется после дайджеста.
- size_check только по L1. REST бирж только для разовой сверки funding/PnL.

## Ops / collector (голова)

Сервер, утренний снимок, скрипты юнитов. Живые ордера и would_send не веду. Архитектура: `docs/`.

- Вектор: держать D зелёным (collector/compact/backup) как базу под would_send canary → 2.2 → **2.3** → 2.4… из [roadmap.md](roadmap.md); параллельно крутится gear23 live (бот, динамический пул).
- Юнит `spread-collector-next`, код `/root/spread_staging`, данные `/data/live`. SHA нет: `/root/spread_staging` не git-checkout, крутится задеплоенное дерево (innovation-фикс discovery = main `aae6695`, backup_transfer = main `0cb24c1`). Старый `spread-collector` inactive/disabled.
- Изменения на хосте за сутки: рестартов юнитов и деплоев кода нет, collector тот же PID 4176949, gear23 тот же PID 1614324. Изменён только unit `spread-bbot-gear23-metrics-compact.service` (11.11 23:23 МСК, флаг `--merge-late-rows`, по «да» Миши). На main за сутки только docs: `810da2a` (ops-снимок 11.11) и `7db4e24` (секция Contour B).
- Статус 12.11 ~09:45 МСК: collector active с 22.09 14:07 МСК (PID 4176949, heartbeat 203 пары, `failures=0`, MemoryCurrent ~2.0 GiB — в основном page cache).
- Backup (rclone в `backup1tb:spread-compacted`) по факту доставки: **D зелёный** — за 24ч 288 ok / 1 fail (норма ~9; `copyto exceeded watchdog timeout 120s` 11.11 22:40 МСК, файл потом доставлен), 0 fail с 00:00 МСК 12.11. Неотправленных в `/data/compacted` 1 (~3 мин), последний успешный transfer 12.11 09:36 МСК.
- Compactor по таймеру без ошибок. Ночной compact 03:15 МСК 12.11: `spread-bbot-gear23-metrics-compact` отработал (days_errors=0, days_skipped=137, exit 0), θ K=1 compact тоже штатно. Топ-10 volatile 12.11 отработал 09:10–09:11 МСК, окно 287/289 слотов.
- `systemctl --failed` пуст. Старые one-shot/canary юниты (`spread-bars-compactor`, `spread-bars-compacted-backup-transfer` с 13.08; `spread-bbot-ev2-10/11/12` с 21–23.09; `gear23-shadow-end-one-hour-20261024t1219` с 24.10) inactive. Не чистил и не сбрасывал.
- Соседи на хосте (не веду): `spread-bbot-would-send-prod` inactive/disabled; gear23 live PID 1614324 standalone с 25.10 13:58 МСК; θ K=1 canary inactive/disabled, oneshot не запускал. HL v2 не запущен.
- Ops 24h: CPU ср. 8% / пик 25%, load сейчас ~0.5, RAM пик 3.1 из 16 GiB (свободно ≥13 GiB), egress 6.6 GiB, пик ~3.2 Мбит/с.
- Лимиты collector: skew/age 2000 мс, HOT_ADD max_extra=8, bars off. Диск 43/79G (56%), свободно ~34G. Данные впервые материализуются в `/data/live`, затем compact в `/data/compacted`, резервная копия — `backup1tb:spread-compacted`.
- Слежу я (голова), утренний дайджест ~9:32 МСК. @бот — Contour B / gear23 live, @стратег — would_send. Sentry ops отдельно, не эта секция.
- Не поднимать старый collector. Чужие data root не чищу без явного OK. Утренний ops-снимок NOW.md коммичу сразу в main. Прочий патч в NOW.md и на сервер — только после явного «да».
