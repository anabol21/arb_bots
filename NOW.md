# NOW

Снимок на 2026-10-16 ~09:40 МСК. Каждая секция — зона своего агента, чужие секции не переписывать.

Вектор развития: [roadmap.md](roadmap.md). Этот файл — снимок, не план.

## would_send (стратег)

Симуляция гира 2.2, `would_send`, `send=false`. Живых ордеров нет. Sentry env `gear22-would-send-canary`.

- Юнит `spread-bbot-would-send-prod`, данные `/data/bbot-would-send-prod`; active с 10.10 23:06 МСК (PID 3261154, NRestarts=0, MemoryCurrent ~0.59 GiB, `CPUQuota=100%`).
- Checkout на хосте `/root/spread_bbot_theta_top10_canary` (симлинк `/root/spread_bbot_would_send_prod`) = `8f942d2` **+ незакоммиченные правки** (`runtime.py`, `theta_screener.py`, `theta_trade_manager.py`, `tw_p50_watcher.py` и тесты). Это продовое дерево с Codex A/B compute-ядра, не чистый main; эффект на живом CPU отдельно не замерен. Не делать `git checkout`/`reset` на хосте.
- Статус 13.10 ~23:00 МСК: пул 66 (база 29 + 37 экстра) после expand-only ротации 10:00 МСК; добавлены DOS и BLUR, `history_seed_done rc=0`, `drop_list=[]`. Heartbeat свежий, `pending=None`, `position=None`; сделки 13.10 (2 открытия, 2 закрытия) в `would_send=True`, `send=False`.
- Гейты: θ_open 0.50, p50_open 0.60, min_profit 0.20, fee 0.30, K=1, notional 20 USDT; tick AGE/SKEW 10000 мс; size gate off (`BBOT_SIZE_GATE=0`, `size_ok_raw` пишется).
- `theta/` и `tw_p50/` закрытых дней сжимаются в `metrics.jsonl.zst` на смене UTC event_date (`BBOT_METRICS_ROTATE_COMPRESS=1`, + startup sweep); `floor/` не сжимаем. За 12.10 закрытые дни сжаты, таймер ротации active.
- В runtime есть `sentry_trade_emit status=ok` на сделках 12–13.10; локальный маркер не доказывает доставку issue в Sentry. Отдельно `fail/skipped` в просмотренном хвосте не найдено.
- Не трогать Contour B, `/data/live`, чужие юниты. Рестарт только когда flat по `theta_trades` (не по heartbeat `position`).

## Contour B (бот)

Контур гира 2.3 (динамический пул) на real net, `send=true`, номинал 10 USDT. Запущен standalone (не systemd).

- Вектор: довести 2.3 до «готово» по [roadmap.md](roadmap.md): hot-add монеты торгуют наравне с базой. Фандинг в `close_min_profit` и PnL пока не учитывается, фикс отложен до бэктеста (2.6 в roadmap): на TRUST за ~32 ч удержания фандинг съел ~105% gross, net ≈ −0.034 USDT.
- Код: работающее дерево `gear23-b2.3-tw-deque-20261010` — копия без `.git`. В нём присутствуют функция плеча 1x и гейт `one_x_unconfirmed` из патча `3c719bc`; точное соответствие commit SHA не проверить. Плечо на биржах этим чтением не подтверждалось.
- Пул 66 = база 29 + 37 hot-add из `/data/bbot-would-send-prod/hot_add_delta.csv` (expand-only). Новая монета до подтверждения плеча 1x стоит на гейте `one_x_unconfirmed` по коду.
- Гейты: θ_open 0.50, p50_open 0.60, min_profit 0.20, fee 0.30, min_θ_close 0.05, K=1; tick AGE/SKEW 10000 мс; `terminal_private`, policy gear22.
- Отчёт 13.10 (~23:00 МСК):
  - Текущий PID 3593028 с 12.10 15:32:26 МСК, standalone `python -m app.bot`, cwd `/root/b-private-b-exp/response-manager-code/gear23-b2.3-tw-deque-20261010`, runroot `/root/b-private-b-exp/response-manager/20261005T194200Z-gear22-longrun`, лог `bbot-gear23-b2.3.log`. Heartbeat свежий; пул 66. Checkpoint: `completed_cycles=3`, `pending=false`, `position={}`, `execution_halt_reason=null`. Это локальное состояние, не REST-подтверждение flat на биржах.
  - Последняя закрытая сделка текущего запуска — RSR 13.10 10:49 МСК. Runtime записал `sentry_trade_emit status=ok` для её открытия и закрытия; `fail/skipped` в просмотренном хвосте нет. Локальный `status=ok` не подтверждает доставку в Sentry; прямого Sentry→Codex webhook/плагина сейчас нет, watch опрашивает журналы раз в час.
  - Пост-трейд `dashboard.html` и `chronometry.json` в runroot не обнаружены, маркеров `chronometry_wrote`/`chronometry_failed` в runtime нет. Причина установлена по работающему дереву: `terminal_private` использует `ThetaTradeManager(place_fn=self._synthetic_live_place)` → `place_live`, а единственный вызов `emit_after_dual_ack` находится в неиспользуемом здесь `private/live_broker.py`. Генератор не вызывался. Подключение отчёта к этому пути и проверка до деплоя остаются перед «готово».
  - `open_window_deadline_utc=null`, процесс активен: нового окна открытий сейчас не требуется. При остановке решение принимать по свежему checkpoint и журналу, без автоматического рестарта.
- Метрики сжимает `spread-bbot-gear23-metrics-compact.timer` раз в сутки в 03:15 МСК (Parquet, JSONL удаляется); таймер active, последний service result=success.
- Слежу я (бот): тихий часовой watch по журналам. Не трогать без явного OK Миши: процесс, лаунчер, runroot, плечи на биржах. Контур не пишет в `/data/live` и в данные would_send-prod, оттуда только читает `hot_add_delta.csv` и history.

## Ops / collector (голова)

Сервер, утренний снимок, скрипты юнитов. Живые ордера и would_send не веду. Архитектура: `docs/`.

- Вектор: держать D зелёным (collector/compact/backup) как базу под would_send canary → 2.2 → **2.3** → 2.4… из [roadmap.md](roadmap.md); параллельно крутятся would_send-prod (стратег), HL v2 (Старшой), gear23 live (бот, tw-deque).
- Юнит `spread-collector-next`, код `/root/spread_staging`, данные `/data/live`. SHA нет: `/root/spread_staging` не git-checkout, крутится задеплоенное дерево (последний деплой — cutover 22.09). Старый `spread-collector` inactive/disabled.
- Статус 16.10 ~09:40 МСК: active с 22.09 14:07 МСК (PID 4176949, NRestarts=0, MemoryCurrent ~1.96 GiB), heartbeat 198 пар, `collect_bars=false`, `failures=0`, `rejected_records=0`, queue=0. В `/data/live` 792 новых parquet за 5 мин; это локальная публикация на VPS.
- Compactor 16.10 09:40 МСК: 300000 строк, `row_count_match=true`, errors=0. Backup 09:41 МСК: `download_verify operation_success=true` для последнего объекта в журнале rclone; прямой проверки remote storage не было. Ночной compact 03:15 МСК: gear23 `days_compacted=4 skipped=34 errors=0` (~4.38G→0.45G), θ K=1 `days_compacted=0 skipped=76 errors=0`. Топ-10 volatile отработал 16.10 09:10–09:17 МСК. Таймеры compact/backup/discovery/ops-metrics/top10/would_send-rotate/gear23-metrics-compact/theta-k1-metrics-compact active.
- В `systemctl --failed` висят старые one-shot/canary юниты (`spread-bars-compactor`, `spread-bars-compacted-backup-transfer` с 13.08; `spread-bbot-ev2-10/11/12` с 21–23.09, disabled). Это давний хвост, не текущий сбой; не чищу без OK.
- Соседи на хосте (не веду): `spread-bbot-would-send-prod` active (PID 3261154, SHA `8f942d2` dirty, пул 67, `position=None`); `spread-collector-hl-v2` active (PID 1970420, SHA `4a76890` dirty, 198/198/88, `dropped=0`); gear23 live standalone — деплой 15.10 ~21:00 МСК (SIGTERM старого PID 3593028 при flat, `completed_cycles=10`; в дерево `gear23-b2.3-tw-deque-20261010` добавлены trade history/observation, бэкап `/root/b-private-b-exp/tmp/gear23-observation-predeploy-20261015T175905Z`), новый PID 14411 с 15.10 21:02 МСК, checkpoint `completed_cycles=0`, `position={}`, пул 67.
- Ops 24h: CPU ср. 16% / пик 44%, load ср. 0.97 / пик 3.1, RAM ср. 2.9 / пик 4.0 из 15.6 GiB, egress 17.2 GiB (пик ~7.0 Мбит/с).
- Лимиты collector: skew/age 2000 мс, HOT_ADD max_extra=8, bars off. Диск 37/79G, свободно ~40G. Данные впервые материализуются в `/data/live`, затем compact в `/data/compacted`, резервная копия — `backup1tb:spread-compacted`; успешный локальный publish сам по себе не доказывает удалённую сохранность.
- Слежу я (голова), утренний дайджест ~9:32 МСК. @бот — Contour B / gear23 live, @стратег — would_send, @Старшой — HL. Sentry ops отдельно, не эта секция.
- Не поднимать старый collector. Чужие data root не чищу без явного OK. Утренний ops-снимок NOW.md коммичу сразу в main. Прочий патч в NOW.md и на сервер — только после явного «да».

## HL v2 (Старшой)

Изолированный контур сбора Bybit/OKX + Hyperliquid. Не prod writer и не Contour B.

- Юнит `spread-collector-hl-v2`, код `/root/spread_hl_v2`, данные `/data/live_hl_v2`. Бэкап `backup1tb:spread-hl-v2` из `/data/compacted_hl_v2`.
- SHA checkout на хосте `4a76890` (ветка `main_hl`, dirty: host-патчи flush, tick-gate, reconnect и universe), не tip main.
- Статус 13.10 ~23:00 МСК: active с 04.10 16:43 МСК (PID 1970420, NRestarts=0, MemoryCurrent ~1.26 GiB). Heartbeat Bybit/OKX/HL 198/198/88, один HL-сокет, `dropped=0`, `spooled_jobs=0`, shard0 ok. В `/data/live_hl_v2` 946 новых parquet за 5 мин. `hl-v2-failed-batches.log` пуст.
- Compactor 13.10 22:56 МСК: 107046 строк, `row_count_match=true`, errors=0. Backup: `verify_final operation_success=true` в журнале; прямой проверки remote storage не было. Compactor/backup/disk-guard timers active; disk-guard видит 37 GiB свободно при пороге 15 GiB.
- Лимиты: MemoryMax 6G, disk-guard 15 GiB, CPUQuota 200%. Flush 100000/45 с, skew/age 2000 мс, компактор 2 мин / 2500M. Один HL-сокет на 88 монет. Реконнект HL = 100, Bybit/OKX остаются на 2.
- Слежу я, часовой watch только на red.
- Не трогать prod `/data/live` и Contour B. CEX-пул не сужать. При scale-up перестраивать knobs до запуска.
