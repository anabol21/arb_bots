# NOW

Снимок на 2026-10-10 ~09:37 МСК. Каждая секция — зона своего агента, чужие секции не переписывать.

Вектор развития: [roadmap.md](roadmap.md). Этот файл — снимок, не план.

## would_send (стратег)

Симуляция гира 2.2, `would_send`, `send=false`. Живых ордеров нет. Sentry env `gear22-would-send-canary`.

- Юнит `spread-bbot-would-send-prod`, данные `/data/bbot-would-send-prod`; active с 10.10 16:23 МСК (PID 3196000, NRestarts=0).
- Checkout на хосте `/root/spread_bbot_theta_top10_canary` (симлинк `/root/spread_bbot_would_send_prod`) = `8f942d2` **+ незакоммиченные правки** (`runtime.py`, `theta_screener.py`, `theta_trade_manager.py`, `tw_p50_watcher.py` и тесты) — Codex A/B оптимизированного compute-ядра, трекинг CPU/качества данных. Прод ≠ чистый main (main ушёл вперёд на Gear 2.3 merge `e9c701d`); не делать `git checkout`/`reset` на хосте, пока Codex не закоммитит.
- Статус 10.10 ~21:40 МСК: пул 61 (база 29 + 32 экстра) после expand-only ротации 10:00 МСК (ежедневно, history seed rc=0). Открытых θ нет (8 сделок, все закрыты).
- Гейты: θ_open 0.50, p50_open 0.60, min_profit 0.20, fee 0.30, K=1, notional 20 USDT; tick AGE/SKEW 10000 мс; size gate off (`BBOT_SIZE_GATE=0`, `size_ok_raw` пишется); `CPUQuota=100%`.
- `theta/` и `tw_p50/` закрытых дней сжимаются в `metrics.jsonl.zst` на смене UTC event_date (`BBOT_METRICS_ROTATE_COMPRESS=1`, + startup sweep); `floor/` не сжимаем. Рост контура ~0.16 GB/день.
- Следит Sentry (issue created) на открытиях.
- Не трогать Contour B, `/data/live`, чужие юниты. Рестарт только когда flat по `theta_trades` (не по heartbeat `position`).

## Contour B (бот)

Контур гира 2.3 (динамический пул) на real net, `send=true`, номинал 10 USDT. Запущен standalone (не systemd), статус и PID — в отчёте 10.10 ниже.

- Вектор: довести 2.3 до «готово» по [roadmap.md](roadmap.md): hot-add монеты торгуют наравне с базой. Фандинг в `close_min_profit` и PnL пока не учитывается, фикс отложен до бэктеста (2.6 в roadmap): на TRUST за ~32 ч удержания фандинг съел ~105% gross, net ≈ −0.034 USDT.
- Код: дерево `gear23-b2.3-tw-deque-20261010` (см. отчёт ниже) заменило прежнее `f230345` + патч `3c719bc` (плечо 1x на hot-add). Входит ли `3c719bc` в tw-deque — не проверено. Запуск standalone, не systemd, лаунчером.
- Пул 59 = база 29 + 30 hot-add из `/data/bbot-would-send-prod/hot_add_delta.csv` (expand-only). Новая монета получает плечо 1x на обеих биржах и до подтверждения стоит на гейте `one_x_unconfirmed`.
- Гейты: θ_open 0.50, p50_open 0.60, min_profit 0.20, fee 0.30, min_θ_close 0.05, K=1; tick AGE/SKEW 10000 мс; `terminal_private`, policy gear22.
- Отчёт 10.10 (~21:40 МСК):
  - gear 2.3 остановился сам 10.10 02:57 МСК (`open_window_elapsed_flat`, `completed_cycles=1`) после закрытия TRUST `ce305e9a`. Рестарт 10.10 18:41:45 МСК: PID 3224101, standalone `python -m app.bot` (не systemd), код `gear23-b2.3-tw-deque-20261010`. cwd `/root/b-private-b-exp/response-manager-code/gear23-b2.3-tw-deque-20261010`, лаунчер `/tmp/gear23-b23-twdeque-launch-20261010.sh`, runroot `/root/b-private-b-exp/response-manager/20261005T194200Z-gear22-longrun`, лог `bbot-gear23-b2.3.log`.
  - В новом коде последние правки Codex: оптимизация вычислительного ядра скользящих квантилей (tw deque). Ожидается, что нагрузка на CPU в Contour B и would_send заметно уменьшится (не замерено до записи; по словам Миши). Без сделок с рестарта, heartbeat свежий (проверено 10.10 21:37 МСК).
  - would_send (стратег): `spread-bbot-would-send-prod` active, PID 3196000, перезапущен 10.10 16:23:01 МСК. Отдельного процесса стратега нет, стратег = would_send.
  - Sentry (проверка 10.10 21:40 МСК): тестовые события из отдельного короткого процесса с теми же настройками дошли: `ARB-BOTS-CONTOUR-B-4V` (проект `arb-bots-contour-b`, env `/etc/spread/bbot-gear22-live-canary.env`) и `ARB-BOTS-STRATEGIST-2D` (проект `arb-bots-strategist`, env `/etc/spread/sentry-strategist.env`). Ключи и проекты рабочие; что sentry инициализирован внутри самих процессов, подтвердит первая реальная сделка. У gear23 в новом лаунчере нет `SENTRY_RELEASE` (не было в файле env). Тестовые issues — игнор.
- Диск: runroot ~4 ГБ, метрики сжимает `spread-bbot-gear23-metrics-compact.timer` раз в сутки в 03:15 МСК (Parquet, JSONL удаляется).
- Ближайшее: подтвердить Sentry-события и пост-трейд дашборд на первой живой сделке; решить, нужно ли новое окно открытий.
- Слежу я (бот): тихий часовой watch и Sentry issue-триггер.
- Не трогать без явного OK Миши: процесс, лаунчер, runroot, плечи на биржах, мёрж и деплой. Контур не пишет в `/data/live` и в данные would_send-prod, оттуда только читает `hot_add_delta.csv` и history.

## Ops / collector (голова)

Сервер, утренний снимок, скрипты юнитов. Живые ордера и would_send не веду. Архитектура: `docs/`.

- Вектор: держать D зелёным (collector/compact/backup) как базу под would_send canary и 2.2/2.3 из [roadmap.md](roadmap.md); параллельно крутятся would_send-prod (стратег), HL v2 (Старшой); ручной gear23 live-прогон (бот) после окна открытий остановился сам.
- Юнит `spread-collector-next`, код `/root/spread_staging`, данные `/data/live`.
- SHA нет: `/root/spread_staging` не git-checkout, крутится задеплоенное дерево (последний деплой — cutover 22.09).
- Статус 10.10 ~09:35 МСК: active с 22.09 14:07 МСК, NRestarts=0, MemoryCurrent ~2.1G (MemoryMax infinity), pairs=198, collect_bars=false, failures=0, rejected=0, свежих файлов в `/data/live` за 5 мин ~396. Топ-10 volatile (OKX–Bybit, rolling 24ч) отработал 10.10 09:10–09:15 МСК (198 монет, слоты 287/289). θ-compact 03:15 МСК: days_compacted=0 / skipped=76 / errors=0. gear23-metrics-compact 03:15 МСК: ok, 4 дня (2026-10-08), ~3.3G→176M JSONL→Parquet (delete_jsonl=True); days_skipped=11. Compactor/backup идут (top-level `/data/compacted` 1 файл, `sent/` 143). Таймеры compact/backup/discovery/ops-metrics/top10/would_send-rotate/gear23-metrics-compact/theta-k1-metrics-compact active. Старый collector inactive/disabled.
- Деплои за сутки (по процессам на хосте): collector-next, would_send-prod и HL v2 без рестартов (ActiveEnterTimestamp без изменений). tip main docs до пульса `ac3064b`. would_send SHA на хосте `8f942d2` (detached). Ротация would_send 09.10 10:00 МСК: newly_added=[] / drop=[], пул остался 59 = 29+30. Ручной gear23 B2.3: процесс PID 2808155 **не жив** — `done_reason=open_window_elapsed_flat`, TRUST long закрыт `close_min_profit`, `completed_cycles=1`, `updated_at` 10.10 ~02:57 МСК; runroot ~4.0G.
- Соседи на хосте (не веду): `spread-bbot-would-send-prod` active с 08.10 15:16 МСК, ~0.53G, SHA `8f942d2`, пул 59, `position=None`; `spread-collector-hl-v2` active с 04.10 16:43 МСК, ~1.16G / 6G, heartbeat 198/198/88, dropped=0, SHA `4a76890` (main_hl dirty). gear23 live: stopped после окна (окно до 09.10 15:27 МСК). `spread-bbot-gear22-live-canary`, `gear2` и `theta-k1` inactive/disabled. 5 старых failed-юнитов (bars-compactor/backup, ev2-10/11/12) — давний хвост.
- Лимиты collector: skew/age 2000 мс, HOT_ADD max_extra=8, bars off. Диск 34/79G, свободно ~42G (`/data/live` 2.0G, `live_hl_v2` ~0.65G, `bbot-would-send-prod` 2.4G, `compacted` 3.8G, gear23 runroot 4.0G). Ops 24ч: CPU avg 24% / peak 52%, load ~1.4 (peak 3.6), RAM 2.6/16G (peak 3.0G), egress 13.7 GiB (peak ~6.1 Mbit/s).
- Слежу я (голова), утренний дайджест ~9:32 МСК. @бот — Contour B / gear23 live, @стратег — would_send, @Старшой — HL. Sentry ops отдельно, не эта секция.
- Не поднимать старый collector. Чужие data root не чищу без явного OK. Утренний ops-снимок NOW.md коммичу сразу в main. Прочий патч в NOW.md и на сервер — только после явного «да».


## HL v2 (Старшой)

Изолированный контур сбора Bybit/OKX + Hyperliquid. Не prod writer и не Contour B.

- Юнит `spread-collector-hl-v2`, код `/root/spread_hl_v2`, данные `/data/live_hl_v2`. Бэкап `backup1tb:spread-hl-v2` из `/data/compacted_hl_v2`.
- SHA checkout на хосте `4a76890` (ветка `main_hl`, dirty: host-патчи flush, tick-gate, reconnect и universe), не tip main.
- Статус 03.10: зелёная, heartbeat 198/198/88. Обрыв HL WS ~12:10–13:56 МСК сам поднялся.
- Лимиты: MemoryMax 6G, disk-guard 15 GiB, CPUQuota 200%. Flush 100000/45 с, skew/age 2000 мс, компактор 2 мин / 2500M. Один HL-сокет на 88 монет. Реконнект HL = 100, Bybit/OKX остаются на 2.
- Слежу я, часовой watch только на red.
- Не трогать prod `/data/live` и Contour B. CEX-пул не сужать. При scale-up перестраивать knobs до запуска.
