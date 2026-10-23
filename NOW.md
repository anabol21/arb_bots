# NOW

Снимок на 2026-10-23 ~09:40 МСК. Каждая секция — зона своего агента, чужие секции не переписывать.

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

- Вектор: держать D зелёным (collector/compact/backup) как базу под would_send canary → 2.2 → **2.3** → 2.4… из [roadmap.md](roadmap.md); параллельно крутится gear23 live (бот, пул 12).
- Юнит `spread-collector-next`, код `/root/spread_staging`, данные `/data/live`. SHA нет: `/root/spread_staging` не git-checkout, крутится задеплоенное дерево. Старый `spread-collector` inactive/disabled.
- Деплои за сутки (22.10 ~09:36 → 23.10 ~09:36 МСК):
  - 22.10 ~21:56 МСК в `/root/spread_staging` поправлены `research/is_crypto.py` и `app/discovery/intersection.py` (innovation-зона Bybit принимается, новый hot-add требует крипто-классификацию OKX); на main это `aae6695`. Collector без рестарта, hot-add дал 203 пары (AXIS, DNA, KII, XDP).
  - 22.10 14:43 МСК остановлен и выключен `spread-bbot-would-send-prod` (inactive/disabled); таймер `spread-bbot-would-send-prod-rotate` остался active и отрабатывает в 10:00 МСК.
  - 22.10 22:19:50 МСК gear23 перезапущен с пулом 12 (PID 1163920, то же дерево tradeobs, лаунчер `/tmp/gear23-pool12-launch.sh`).
- Статус 23.10 ~09:36 МСК: active с 22.09 14:07 МСК (PID 4176949, NRestarts=0, MemoryCurrent ~2.0 GiB), heartbeat 203 пары, `collect_bars=false`, `failures=0`, `rejected_records=0`, queue=0, `ws_unrecovered=0`. В `/data/live` 812 новых parquet за 5 мин; это локальная публикация на VPS.
- Compactor 23.10 09:36 МСК: `compaction_complete`, `deferred_archives_remain=0`. Backup: rclone в `backup1tb:spread-compacted` `operation_success=true`. Ночной compact 03:15 МСК: gear23 `days_compacted=5 skipped=60 errors=0` (~4.9G→0.82G), θ K=1 `days_compacted=0 skipped=76 errors=0`. Топ-10 volatile отработал 23.10 09:10–09:16 МСК. Таймеры compact/backup/discovery/ops-metrics/top10/would_send-rotate/gear23-metrics-compact/theta-k1-metrics-compact active.
- В `systemctl --failed` висят старые one-shot/canary юниты (`spread-bars-compactor`, `spread-bars-compacted-backup-transfer` с 13.08; `spread-bbot-ev2-10/11/12` с 21–23.09, disabled). Это давний хвост, не текущий сбой; не чищу без OK.
- Соседи на хосте (не веду): `spread-bbot-would-send-prod` inactive/disabled с 22.10 14:43 МСК (checkout `8f942d2` dirty остаётся, данные `/data/bbot-would-send-prod` 5.8G); gear23 live PID 1163920 standalone, пул 12 (`WAL,RVN,LA` + 9 hot-add), heartbeat свежий, `pending=False`, `position=None`, сделок после рестарта нет, flat. θ K=1 canary inactive/disabled, oneshot не запускал.
- Ops 24h: CPU ср. 7% / пик 25%, load ср. 0.40 / пик 1.5, RAM ср. ~2.1 / пик 3.0 из 15.6 GiB, egress 7.9 GiB.
- Лимиты collector: skew/age 2000 мс, HOT_ADD max_extra=8, bars off. Диск 39/79G, свободно ~37G. Данные впервые материализуются в `/data/live`, затем compact в `/data/compacted`, резервная копия — `backup1tb:spread-compacted`.
- Слежу я (голова), утренний дайджест ~9:32 МСК. @бот — Contour B / gear23 live, @стратег — would_send. Sentry ops отдельно, не эта секция.
- Не поднимать старый collector. Чужие data root не чищу без явного OK. Утренний ops-снимок NOW.md коммичу сразу в main. Прочий патч в NOW.md и на сервер — только после явного «да».
