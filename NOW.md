# NOW

Снимок на 2026-10-30 ~09:40 МСК. Каждая секция — зона своего агента, чужие секции не переписывать.

Вектор развития: [roadmap.md](roadmap.md). Этот файл — снимок, не план.

## would_send (стратег)

Симуляция гира 2.2, `would_send`, `send=false`. Живых ордеров нет. Sentry env `gear22-would-send-canary`.

- Юнит `spread-bbot-would-send-prod`, данные `/data/bbot-would-send-prod` (`theta/`, `tw_p50/`, `theta_trades/`, `floor/`); active с 10.10 23:06 МСК, PID 3261154, NRestarts=0, `CPUQuota=100%`, `MemoryMax=2G`.
- Checkout на хосте `/root/spread_bbot_theta_top10_canary` (симлинк `/root/spread_bbot_would_send_prod`) = `8f942d2` **+ незакоммиченные правки Codex A/B** (`runtime.py`, `theta_screener.py`, `theta_trade_manager.py`, `tw_p50_watcher.py` и тесты). Прод ≠ main. Не делать `git checkout`/`reset` на хосте.
- Статус 20.10.2026 10:40 МСК: пул 71 (база 29 + 42 экстра), expand-only ротация 10:00 МСК (20.10 добавлены METIS, OPG, `history_seed_done rc=0`, drop пуст); потолок экстра `BBOT_HOT_ADD_MAX_EXTRA=48` (макс. 77). Heartbeat свежий, `pending=None`, `position=None`; по `theta_trades` flat.
- Память: MemoryCurrent ~1.24 GiB, но это в основном page cache (~0.94 GiB file); RSS процесса ~310 MiB (anon ~300), рост ~1–6 MiB/сутки, пропорционален пулу, утечки не видно. Рестарт по памяти не нужен.
- Гейты: θ_open 0.50, p50_open 0.60, min_profit 0.20, fee 0.30, K=1, notional 20 USDT; tick AGE/SKEW 10000 мс; size gate off (`BBOT_SIZE_GATE=0`); `send=false` (broker stub).
- `theta/` и `tw_p50/` закрытых дней сжимаются в `metrics.jsonl.zst` на смене UTC (`BBOT_METRICS_ROTATE_COMPRESS=1`); 19.10 сжат.
- Следит: Sentry-рутина стратега.
- Не трогать Contour B, `/data/live`, collector, чужие юниты. Рестарт только когда flat по `theta_trades` (не по heartbeat `position`).

## Contour B (бот)

Контур гира 2.3 (динамический пул) на real net, `send=true`, номинал 10 USDT. Live canary gear23 (B2.3), запущен standalone (не systemd). Планы и вектор — в [roadmap.md](roadmap.md), здесь только состояние.

- Статус 22.10.2026 ~22:30 МСК: PID 1163920 с 22.10 22:19:50 МСК (до этого PID 543732 с 18.10 20:45 МСК), standalone `python -m app.bot`, лаунчер `/tmp/gear23-pool12-launch.sh`, runroot `/root/b-private-b-exp/response-manager/20261005T194200Z-gear22-longrun`, лог `bbot-gear23-b2.3.log` (время в логе UTC). Сейчас flat.
- Код без изменений: дерево `/root/b-private-b-exp/response-manager-code/gear23-b2.3-tw-deque-20261010-tradeobs-20261018T1741Z` (tradeobs: `place_send.py`, `trade_history.py`, `theta_trade_manager.py` + тесты trade-observation).
- Пул сокращён 77 → 12 по решению Миши (только монеты со сделками 05–22.10): база `BBOT_COINS=WAL,RVN,LA` + 9 hot-add из замороженной копии `$RUNROOT/state/pool12/hot_add_delta_pool12.csv` (API3, BLUR, CT, DOOD, IOST, MEW, RSR, SAND, TRUST), `MAX_EXTRA=9`. Прод `/data/bbot-would-send-prod/hot_add_delta.csv` не менялся; новые монеты из ротации would_send в канарейку больше не приезжают. Откат на 77: `/tmp/gear23-rollback77-launch.sh` через `/root/pool12/restart_with_fix1.sh`.
- Остальное без изменений: hot-add с плечом 1x, AGE/SKEW 10 s, flat non-resume, Sentry trade events. Гейты: θ_open 0.50, p50_open 0.60, min_profit 0.20, fee 0.30, min_θ_close 0.05, K=1; `terminal_private`, policy gear22.
- Сделки после рестарта 18.10:
  - SAND long `d6d91247` закрыт 19.10 09:33 МСК, net +$0.015; CT long `57ae1236` 19.10 10:43 → 16:34 МСК, net +$0.031.
  - MEW long `5d423ce8` 21.10 10:01 → 11:00 МСК.
  - 21–22.10: IOST long `44d96435` (12:43 → 14:12 МСК), DOOD long `9e1d3768`, IOST short `88b3522a`, IOST long `7e290c36`, `3cfb536a`, `5a403dbd`. Последняя — IOST long `5a403dbd`, закрыта 22.10 07:46 МСК, net −$0.023 с фандингом.
- Итог 05–22.10: 28 открытий / 28 закрытий по 12 монетам; net без фандинга +$0.57, с записанным фандингом −$0.07 (фандинг пишется только с 19.10). Отчёт: `/workspace/pool_signal_distribution_20261022` (на машине бота).
- Открытые вопросы: journal fill ~+1.05 с после сигнала при биржевом исполнении +25–40 мс (повторный фрейм Bybit private, `wait_fill` ждёт его); после рестарта `ws_tasks_started n=26` при `expect=24` — наблюдаем.
- Метрики сжимает `spread-bbot-gear23-metrics-compact.timer` раз в сутки в 03:15 МСК (Parquet, JSONL удаляется); работает ежедневно.
- Слежу я (бот): часовой watch в :47 МСК + Sentry-рутина, дашборд после каждой сделки. Не трогать без явного OK Миши: процесс, лаунчер, runroot, плечи на биржах. Контур не пишет в `/data/live` и в данные would_send-prod.

## Ops / collector (голова)

Сервер, утренний снимок, скрипты юнитов. Живые ордера и would_send не веду. Архитектура: `docs/`.

- Вектор: держать D зелёным (collector/compact/backup) как базу под would_send canary → 2.2 → **2.3** → 2.4… из [roadmap.md](roadmap.md); параллельно крутится gear23 live (бот, динамический пул).
- Юнит `spread-collector-next`, код `/root/spread_staging`, данные `/data/live`. SHA нет: `/root/spread_staging` не git-checkout, крутится задеплоенное дерево (innovation-фикс discovery = main `aae6695`). Старый `spread-collector` inactive/disabled.
- Изменения на хосте с 29.10 ~09:40 МСК: деплоев и рестартов нет, systemd-юниты те же, gear23 тот же PID 1614324. Пул gear23 вырос 25 → 29 через `spread-canary-pool-dyn-append` (29.10: BLEND, INIT, DOS, RECALL). Из `/tmp` удалены `exp4_full_rs_20261025a` и `gear23-core-opt-20261023`, journald ужат до ~1G. main без коммитов после `442417d`.
- Статус 30.10 ~09:41 МСК: active с 22.09 14:07 МСК (PID 4176949, NRestarts=0, MemoryCurrent ~3.3 GiB, в основном page cache), heartbeat 203 пары, `collect_bars=false`, `failures=0`, `rejected_records=0`, `ws_unrecovered=0`, queue=0. В `/data/live` 1015 новых parquet за 5 мин; это локальная публикация на VPS.
- Compactor и backup (rclone в `backup1tb:spread-compacted`) отрабатывают по таймерам без ошибок. Ночной compact 03:15 МСК: gear23 `days_compacted=4 skipped=87 errors=0`, θ K=1 `days_compacted=0 skipped=76 errors=0`. Топ-10 volatile 30.10 отработал 09:10–09:16 МСК. Таймеры compact/backup/discovery/ops-metrics/top10/would_send-rotate/gear23-metrics-compact/theta-k1-metrics-compact/canary-pool-dyn-append active.
- В `systemctl --failed` старые one-shot/canary юниты (`spread-bars-compactor`, `spread-bars-compacted-backup-transfer` с 13.08; `spread-bbot-ev2-10/11/12` с 21–23.09; `gear23-shadow-end-one-hour-20261024t1219` с 24.10). Это хвосты, не сбой; не чищу без OK.
- Соседи на хосте (не веду): `spread-bbot-would-send-prod` inactive/disabled с 22.10 14:43 МСК (checkout `8f942d2` dirty, данные `/data/bbot-would-send-prod` 5.8G, rotate-таймер active); gear23 live PID 1614324 standalone с 25.10 13:58 МСК, база `WAL,RVN,LA` + 26 hot-add из `state/pool_dyn/hot_add_delta_dyn.csv` (`MAX_EXTRA=57`), за сутки закрыто 5 сделок (AXIS ×2, BLUR ×2, IOST), последняя AXIS `1f01a2f2` закрыта 30.10 04:43 МСК, сейчас flat; θ K=1 canary inactive/disabled, oneshot не запускал.
- Ops 24h: CPU ср. 8% / пик 30%, load ср. 0.49 / пик 1.8, RAM ср. ~2.2 / пик 3.4 из 15.6 GiB, egress 7.9 GiB.
- Лимиты collector: skew/age 2000 мс, HOT_ADD max_extra=8, bars off. Диск 40/79G (53%), свободно ~36G; `/data` 20G, `/tmp` 4.9G, `/var/log` 4.6G. Данные впервые материализуются в `/data/live`, затем compact в `/data/compacted`, резервная копия — `backup1tb:spread-compacted`.
- Слежу я (голова), утренний дайджест ~9:32 МСК. @бот — Contour B / gear23 live, @стратег — would_send. Sentry ops отдельно, не эта секция.
- Не поднимать старый collector. Чужие data root не чищу без явного OK. Утренний ops-снимок NOW.md коммичу сразу в main. Прочий патч в NOW.md и на сервер — только после явного «да».
