# NOW

Снимок на 2026-10-10 ~09:37 МСК. Каждая секция — зона своего агента, чужие секции не переписывать.

Вектор развития: [roadmap.md](roadmap.md). Этот файл — снимок, не план.

## would_send (стратег)

Симуляция гира 2.2, `would_send`, `send=false`. Живых ордеров нет.

- Юнит `spread-bbot-would-send-prod`, данные `/data/bbot-would-send-prod`.
- SHA checkout на хосте `8f942d2` (код #75 `BBOT_SIZE_GATE=0` + #76/#77 zstd-компактор; tip main).
- Статус 07.10 ~00:39 МСК: выкатили `8af4d52` (detached) поверх `c3d3e5f`, рестарт только `spread-bbot-would-send-prod` на flat book (`position=None` / orphan θ нет). Size gate на stub **выключен**: unit env `BBOT_SIZE_GATE=0` — `size_check` всё ещё пишет available/planned (`size_ok_raw`), но `size_ok=true` и не блокирует open/close. Live/gear23 не трогали (default gate on). `BBOT_NOTIONAL_USDT=20`, AGE/SKEW 10s (`SPREAD_TICK_AGE_MAX_MS=10000` / `SPREAD_TICK_SKEW_MAX_MS=10000`). Ротация expand-only в 10:00 МСК. Пул 54 = база 29 + 25 экстра (AEON, API3, ARX, BREV, CT, DGAI, ENJ, ESP, GMX, KGEN, LAB, LQTY, MANA, OPN, RECALL, RESOLV, RSR, SAND, SENT, SPACE, TRIA, TRUTH, UMA, WOO, YGG). Открытых θ нет. Канарейка theta-k1 остановлена. После деплоя: `insufficient_size` на stub не должен резать сделки; смотреть opens/heartbeat.
- Гейты: θ_open 0.50, p50_open 0.60, min_profit 0.20, fee 0.30, K=1; tick AGE/SKEW 10000 мс; **size gate off** (`BBOT_SIZE_GATE=0`).
- `theta/` и `tw_p50/` закрытых дней — `metrics.jsonl.zst` (zstd на смене UTC event_date, grace 120 с, + startup sweep; `floor/` не сжимаем). Флаг unit `BBOT_METRICS_ROTATE_COMPRESS=1`.
- Следит Sentry (issue created) на открытиях.
- Не трогать Contour B и `/data/live`. Шов рестарта: hot-add экстра прогревается из устаревшей history, floor прыгает.

## Contour B (бот)

Живой торговый контур гира 2.3 (динамический пул) на real net, `send=true`, номинал 10 USDT. Единственный процесс с живыми ордерами на хосте.

- Вектор: довести 2.3 до «готово» по [roadmap.md](roadmap.md), то есть hot-add монеты торгуют наравне с базой. Плечо 1x на hot-add закрыто 08.10. Ближайшее: (1) подтвердить Sentry trade-события и пост-трейд дашборд (наши метки vs биржевые fill, PnL наш vs биржевой) на первой живой сделке; (2) решить, что делать с окном открытий после 09.10 15:27 МСК; (3) проверить первый прогон компактора 09.10 03:15 МСК и решить про удаление JSONL. Дальше 2.4: приват только по волатильному кластеру.
- Вектор, порядок планов (со слов Михаила, 10.10): gear 2.4 — на этих выходных (10–11.10); gear 2.5 — на следующей неделе; затем gear 2.6 (режим/критерий M, детали критерия M — по линии стратега, уточняются). В рамках 2.6 запланированы: (1) бэктест фандинга Bybit/OKX на gear 2.6 (см. пункт ниже: оценка случайности + Монте-Карло); (2) по его результатам — решение по параметру `expected_funding_bps` или лимиту времени удержания; (3) отдельной задачей — учёт фандинга в коде gear23 (см. пункт ниже, заглушен до результатов бэктеста). Замороженные knobs gear 2.6 для бэктеста: θ_open 0.50, p50_open 0.60, min_profit 0.20, fee 0.30, min_θ_close 0.05, K=1. Статус: запланировано, не начато.
- Вектор, позже (после 2.5; в списке 2.5 пока нет, ставлю в конец): бэктест фандинга на gear 2.6. Постановка: на полной дате бэктестов (~2 месяца) собрать информацию о фандингах Bybit и OKX по всем смоделированным сделкам gear 2.6 (`research.gear22_backtest.policy.decide()`, frozen knobs: θ_open 0.50, p50_open 0.60, min_profit 0.20, fee 0.30, min_θ_close 0.05, K=1): ставка и знак на каждом сетлменте (интервал у обеих бирж 4 ч), сумма в USDT и в bps за сделку, по каждой ноге. Два направления проработки: (а) попробовать не учитывать случайность фандинга на дистанции сделок и оценить, допустимо ли это (уравновешивает ли фандинг на дистанции: плюс на одной ноге против минуса на другой, или это систематический риск); (б) задизайнить учёт фандинга по типу Монте-Карло на бэктесте: для каждой сделки моделировать распределение фандинга по историческим ставкам и смотреть, какую дисперсию и смещение PnL он даёт на сделке и на всём наборе; по итогу решить, нужен ли параметр `expected_funding_bps` вместо одного fee=0.30 или ограничение времени удержания. Контекст: TRUST long `ce305e9a` (08–10.10, ~32 ч hold): фандинг Bybit −0.05445 USDT (8 списаний, wire execType=Funding) + OKX ≈ −0.004 USDT (оценка по публичной ставке) съел ~105% gross +0.0558, net ≈ −0.034 вместо +0.0241. Фикс учёта фандинга в коде gear23 (close_min_profit/`potential_pp`, `pnl_usdt_approx` и отчёты PnL, разбор funding-сообщений в `ws_private.py`, которые сейчас проходят как обычный order_update и ни на что не влияют; подписка на OKX account/bills) ЗАГЛУШЕН: делается после бэктеста, отдельной задачей. Статус: не начато, только запланировано.
- Процесс: standalone, не systemd. PID 2808155 с 08.10 15:49 МСК, `python -m app.bot.runtime`. Код `/root/b-private-b-exp/response-manager-code/response-handler-20261006-gear23-B2.3-f230345/gear23`: `f230345` (ветка `codex/gear23-dynamic-pool`, не main) + патч `3c719bc` поверх на месте (`app/bot/hot_add.py`, `runtime.py`, `private/leverage_one.py`); `3c719bc` пока нет в origin. Runroot `/root/b-private-b-exp/response-manager/20261005T194200Z-gear22-longrun`, лог `bbot-gear23-b2.3.log`.
- Статус 10.10: gear23 PID 2808155 остановился сам 10.10 02:57 МСК (`open_window_elapsed_flat`) после закрытия TRUST `ce305e9a`, `completed_cycles=1`; живого Contour B сейчас нет.
- Лаунчер `/tmp/gear23_launch_hotadd_leverage_3c719bc_20261008.py`, откат `/tmp/gear23_launch_f230345_tick10s_sentry_20261007T092728Z.py`. Гарды лаунчера: flat и без pending, совпадение дедлайна окна, confirmed 1x пул = 54, хэши `ws_private`/`ws_warm_loop`/`ws_warm_session`. Юнит `spread-bbot-gear22-live-canary` disabled с 16.09 и к контуру не относится.
- Статус 08.10 16:05 МСК: зелёный, flat, ошибок нет, прогрев 120 warm_ok. После рестарта плечо 1x подтверждено на OKX и Bybit для NMR, TRUST, CASHCAT, CP, MET, все стали eligible, `one_x_unconfirmed` не встречается. С рестарта сделок не было. Последняя: API3 long 07.10 03:33→06:41 МСК, net ≈ +$0.017.
- Пул 59 = база 29 + 30 hot-add из `/data/bbot-would-send-prod/hot_add_delta.csv` (expand-only, опрос 30 с, max_extra 48, прогрев из `/data/bbot-would-send-prod-history`). Новая hot-add монета сама получает плечо 1x через REST set + readback на обеих биржах (`BBOT_HOT_ADD_SET_LEVERAGE=1`) и до подтверждения стоит на гейте `one_x_unconfirmed`. База 54 подтверждена 1x-prep на старте (`BBOT_CONFIRMED_1X_COINS`).
- Гейты: θ_open 0.50, p50_open 0.60, min_profit 0.20, fee 0.30, min_θ_close 0.05, K=1; tick AGE/SKEW 10000 мс; `terminal_private`, policy gear22; окно открытий до 09.10 15:27 МСК (`open_window_deadline_utc` в `canary_state.json`).
- Последние патчи: 06.10 рестарт на `f230345` (hot-add под TaskSupervisor, гейт `one_x_unconfirmed`); 06.10 AGE/SKEW 2 с → 10 с (duty hot-add ~37% → ~96%); 07.10 12:28 Sentry DSN в лаунчер (`SENTRY_*` из `/etc/spread/bbot-gear22-live-canary.env`, проект `arb-bots-contour-b`); 08.10 15:49 `3c719bc`: leverage 1x на hot-add + рестарт flat; 08.10 юнит компактора `spread-bbot-gear23-metrics-compact` по образцу would_send/theta-k1. В работе, не в проде: дизайн дашборда `/workspace/specs/post-trade-dashboard-design-2026-10-07.md` на боксе бота.
- Диск: runroot 7,7 ГБ, JSONL растёт ~3,3 ГБ/сутки (почти всё `data/theta` и `data/tw_p50`). Компактор `spread-bbot-gear23-metrics-compact.timer` раз в сутки в 03:15 МСК (00:15 UTC), первый прогон 09.10: закрытые UTC-дни `theta,tw_p50,floor,theta_trades` → Parquet, `--keep-days 1 --min-age-hours 12`, пока `--no-delete-jsonl` (JSONL не удаляется, в отличие от theta-k1 с `--delete-jsonl`). Лог `/var/log/spread/bbot-gear23-metrics-compact.log`, MemoryMax 1G, Nice 10, private journal и runtime не трогает.
- Слежу я (бот): часовой тихий watch в :47 МСК, Sentry issue-триггер. Пишу Мише только о сделках и проблемах.
- Не трогать без явного OK Миши: процесс, лаунчер, runroot, плечи на биржах, мёрж/деплой. Contour B не пишет в `/data/live` и в данные would_send-prod, оттуда только читает `hot_add_delta.csv` и history.

## Ops / collector (голова)

Сервер, утренний снимок, скрипты юнитов. Живые ордера и would_send не веду. Архитектура: `docs/`.

- Вектор: держать D зелёным (collector/compact/backup) как базу под would_send canary и 2.2/2.3 из [roadmap.md](roadmap.md); параллельно крутятся would_send-prod (стратег), HL v2 (Старшой); ручной gear23 live-прогон (бот) после окна открытий остановился сам. 2.5/3 / полный пул / bars on / live без команды — не трогаем.
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
