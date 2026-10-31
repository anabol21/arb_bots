# NOW

Снимок на 2026-10-31 ~09:40 МСК. Каждая секция — зона своего агента, чужие секции не переписывать.

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

- gear23 / B2.3 live canary, PID 1614324, работает с 25.10 13:58 МСК, рестартов и halts нет.
- Пул 35 из 60 (3 базовые + 32 extras), ротация ежедневно в 10:10 МСК; 31.10 добавлены WET, ESP, DNA, 30.10 — PARTI, SOPH, LQTY. Нет подтверждения 1x в логе у APR и BLEND.
- Позиция на 31.10 ~11:40 МСК: WET long ed510209 (вход 10:23).
- completed_cycles 28 на конец 30.10.
- За 29.10: 4 сделки, net +$0.1979. За 30.10: 3 сделки, net +$0.0293. Funding Bybit сходится с closedPnl.
- Мониторинг: вместо уведомлений по каждой сделке утренний дайджест ~10:30 МСК (учёт в %, задержки, спреды, пул-репорт). Остальные watch на паузе.
- size_check только по L1. REST бирж только для разовой сверки funding/PnL.

## Ops / collector (голова)

Сервер, утренний снимок, скрипты юнитов. Живые ордера и would_send не веду. Архитектура: `docs/`.

- Вектор: держать D зелёным (collector/compact/backup) как базу под would_send canary → 2.2 → **2.3** → 2.4… из [roadmap.md](roadmap.md); параллельно крутится gear23 live (бот, динамический пул).
- Юнит `spread-collector-next`, код `/root/spread_staging`, данные `/data/live`. SHA нет: `/root/spread_staging` не git-checkout, крутится задеплоенное дерево (innovation-фикс discovery = main `aae6695`). Старый `spread-collector` inactive/disabled.
- Изменения на хосте с 30.10 ~09:40 МСК: деплоев и рестартов нет, systemd-юниты те же, gear23 тот же PID 1614324. Пул gear23 вырос 29 → 32 через `spread-canary-pool-dyn-append` (30.10: PARTI, SOPH, LQTY). main без коммитов после `9dc8827`.
- Статус 31.10 ~09:40 МСК: active с 22.09 14:07 МСК (PID 4176949, NRestarts=0, MemoryCurrent ~2.0 GiB, в основном page cache), heartbeat 203 пары, `collect_bars=false`, `failures=0`, `rejected_records=0`, `ws_unrecovered=0`, queue=0. В `/data/live` 406 новых parquet за 5 мин; это локальная публикация на VPS.
- Compactor и backup (rclone в `backup1tb:spread-compacted`) отрабатывают по таймерам без ошибок. Ночной compact 03:15 МСК: gear23 `days_compacted=4 skipped=91 errors=0`, θ K=1 `days_compacted=0 skipped=76 errors=0`. Топ-10 volatile 31.10 отработал 09:10–09:16 МСК. Таймеры compact/backup/discovery/ops-metrics/top10/would_send-rotate/gear23-metrics-compact/theta-k1-metrics-compact/canary-pool-dyn-append active.
- В `systemctl --failed` старые one-shot/canary юниты (`spread-bars-compactor`, `spread-bars-compacted-backup-transfer` с 13.08; `spread-bbot-ev2-10/11/12` с 21–23.09; `gear23-shadow-end-one-hour-20261024t1219` с 24.10). Это хвосты, не сбой; не чищу без OK.
- Соседи на хосте (не веду): `spread-bbot-would-send-prod` inactive/disabled с 22.10 14:43 МСК (checkout `8f942d2` dirty, данные `/data/bbot-would-send-prod` 5.8G, rotate-таймер active); gear23 live PID 1614324 standalone с 25.10 13:58 МСК, база `WAL,RVN,LA` + 29 hot-add из `state/pool_dyn/hot_add_delta_dyn.csv` (`MAX_EXTRA=57`), за сутки закрыто 4 сделки (BLUR ×2, SOPH ×2), последняя SOPH `5c8ae4d2` закрыта 31.10 08:31 МСК, сейчас flat; θ K=1 canary inactive/disabled, oneshot не запускал.
- Ops 24h: CPU ср. 8% / пик 28%, load ср. 0.43 / пик 1.6, RAM ср. ~2.2 / пик 3.3 из 15.6 GiB, egress 7.3 GiB.
- Лимиты collector: skew/age 2000 мс, HOT_ADD max_extra=8, bars off. Диск 41/79G (54%), свободно ~35G; `/data` 20G, `/tmp` 4.9G, `/var/log` 5.0G. Данные впервые материализуются в `/data/live`, затем compact в `/data/compacted`, резервная копия — `backup1tb:spread-compacted`.
- Слежу я (голова), утренний дайджест ~9:32 МСК. @бот — Contour B / gear23 live, @стратег — would_send. Sentry ops отдельно, не эта секция.
- Не поднимать старый collector. Чужие data root не чищу без явного OK. Утренний ops-снимок NOW.md коммичу сразу в main. Прочий патч в NOW.md и на сервер — только после явного «да».
