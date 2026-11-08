# NOW

Снимок на 2026-11-08 ~09:40 МСК. Каждая секция — зона своего агента, чужие секции не переписывать.

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

- gear23 / B2.3 live canary, PID 1614324, работает с 25.10 13:58 МСК (~12.9 сут), рестартов и halts нет, новых ERROR/Traceback нет.
- Пул 46 из 60 (3 базовые WAL, RVN, LA + 43 extras); 05.11 добавлены HUMA, EWT, ARX, 06.11 — XTZ, SPACE; 07.11 ротация в 10:10 МСК ничего не добавила (9 из топ-10 уже в пуле, AKE пропущен: no_complete_meta). XTZ: 1x подтверждён, но после hot-add warm не прошёл (history_missing, gate floor_warm), повторного warm в логе нет. Нет подтверждения 1x в логе у APR, BLEND, ACU, PARTI.
- Позиций нет (heartbeat 07.11 10:31 МСК: position=None).
- completed_cycles 35.
- 06.11: сделок нет, net $0; ERROR/Traceback за сутки 0; funding проверять нечего.
- Мониторинг: утренний дайджест ~10:30 МСК, эта секция обновляется после дайджеста.
- size_check только по L1. REST бирж только для разовой сверки funding/PnL.

## Ops / collector (голова)

Сервер, утренний снимок, скрипты юнитов. Живые ордера и would_send не веду. Архитектура: `docs/`.

- Вектор: держать D зелёным (collector/compact/backup) как базу под would_send canary → 2.2 → **2.3** → 2.4… из [roadmap.md](roadmap.md); параллельно крутится gear23 live (бот, динамический пул).
- Юнит `spread-collector-next`, код `/root/spread_staging`, данные `/data/live`. SHA нет: `/root/spread_staging` не git-checkout, крутится задеплоенное дерево (innovation-фикс discovery = main `aae6695`, backup_transfer = main `0cb24c1`). Старый `spread-collector` inactive/disabled.
- Изменения на хосте за сутки: деплоев и рестартов юнитов нет, collector тот же PID 4176949, gear23 тот же PID 1614324. Пул gear23 без изменений (46; 07.11 10:10 МСК pool-dyn append ничего не добавил — delta по-прежнему заканчивается XTZ, SPACE от 06.11). 08.11 append в 10:10 МСК ещё впереди. На main за сутки только docs: `96b12c2` (ops-снимок 07.11) и `6f0e197` (секция Contour B после дайджеста бота).
- Статус 08.11 ~09:40 МСК: active с 22.09 14:07 МСК (PID 4176949, NRestarts=0, MemoryCurrent ~1.7 GiB, в основном page cache), heartbeat 203 пары, `collect_bars=false`, `failures=0`, `rejected_records=0`, queue=0. В `/data/live` 609 новых parquet за 5 мин.
- Backup (rclone в `backup1tb:spread-compacted`) по факту доставки: за 24ч 285 ok / 4 fail (норма ~9; 3 файла, все copyto watchdog 120s, все позже ушли: 07.11 19:42, 22:55/23:00 и 23:15 МСК; с 00:00 МСК 08.11 — 0 fail). В `/data/compacted` 1 pending ~3 мин, последний успешный transfer 08.11 09:36 МСК.
- Compactor по таймеру без ошибок. Ночной compact 03:15 МСК: gear23 `days_compacted=3 skipped=119 errors=0`; θ K=1 `days_compacted=0 skipped=76 errors=0`. Топ-10 volatile 08.11 отработал 09:10–09:11 МСК, окно 285/289 слотов. Таймеры compact/backup/discovery/ops-metrics/top10/gear23-metrics-compact/theta-k1-metrics-compact/canary-pool-dyn-append active.
- В `systemctl --failed` сейчас пусто; сами старые one-shot/canary юниты (`spread-bars-compactor`, `spread-bars-compacted-backup-transfer` с 13.08; `spread-bbot-ev2-10/11/12` с 21–23.09; `gear23-shadow-end-one-hour-20261024t1219` с 24.10) inactive. Не чистил и не сбрасывал.
- Соседи на хосте (не веду): `spread-bbot-would-send-prod` inactive/disabled (данные `/data/bbot-would-send-prod` 5.8G); gear23 live PID 1614324 standalone с 25.10 13:58 МСК (~13.8 сут), база `WAL,RVN,LA` + 43 hot-add (`MAX_EXTRA=57`), пул 46, открыта BLEND long 10 USDT с 08.11 04:58 МСК (trade_id `a8fcc753`, на 09:39 МСК ещё открыта); θ K=1 canary inactive/disabled, oneshot не запускал.
- Ops 24h: CPU ср. 5% / пик 24%, load сейчас ~0.3, RAM 2.3 / пик 3.0 из 16 GiB (свободно ≥13 GiB), egress 4.7 GiB / пик 5.0 Mbit/s.
- Лимиты collector: skew/age 2000 мс, HOT_ADD max_extra=8, bars off. Диск 41/79G (55%), свободно ~35G; `/data` 17G, `/tmp` 4.9G, `/var/log` 6.3G. Данные впервые материализуются в `/data/live`, затем compact в `/data/compacted`, резервная копия — `backup1tb:spread-compacted`.
- Слежу я (голова), утренний дайджест ~9:32 МСК. @бот — Contour B / gear23 live, @стратег — would_send. Sentry ops отдельно, не эта секция.
- Не поднимать старый collector. Чужие data root не чищу без явного OK. Утренний ops-снимок NOW.md коммичу сразу в main. Прочий патч в NOW.md и на сервер — только после явного «да».
