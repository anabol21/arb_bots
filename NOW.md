# NOW

Снимок на 2026-11-28 ~09:40 МСК. Каждая секция — зона своего агента, чужие секции не переписывать.

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

- gear23 / B2.3 live canary, pool11 dyn-лаунчер, обёртка PID 1222650, бот 1222651, запущен 23.11 17:38 МСК (аптайм ~4 сут 17 ч на 28.11 10:41); рестартов нет, halts нет, ERROR/Traceback за 27.11 нет.
- Пул 30 из 60 (база 7 + 23 extras: seed AXIS, BTW, CP, KII; 24.11 SAHARA, AEON, DOS, ACU, ENSO, SAPIEN, DNA; 25.11 BIGTIME, AT, HMSTR; 26.11 MOVE, ME, CORE; 27.11 BABY, KGEN, 0G, TRUST; 28.11 10:10 МСК BLUR, SPACE; пропусков нет). 1x подтверждён у BLUR и SPACE, warm live_catchup ok; 0G не прогрет (history_missing), ошибок hot_add нет.
- Позиций нет (heartbeat 28.11 10:41 МСК: position=None).
- completed_cycles 15 (+4 за 27.11).
- 27.11: 4 сделки (ME×2, INIT, YB), net $+0.0131 (среднее +0.0033): гросс +0.2397, комиссии -0.1185, funding -0.1081 (OKX +0.3615, Bybit -0.4696, съел Bybit); Bybit funding сходится с closedPnl и private WS, OKX только по журналу. insufficient_size 1, send_abort 0, обрывов private WS 5 (рядом со сделками нет).
- Мониторинг: утренний дайджест ~10:30 МСК, эта секция обновляется после дайджеста.
- size_check только по L1. REST бирж только для разовой сверки funding/PnL.

## Ops / collector (голова)

Сервер, утренний снимок, скрипты юнитов. Живые ордера и would_send не веду. Архитектура: `docs/`.

- Вектор: держать D зелёным (collector/compact/backup) как базу под would_send canary → 2.2 → 2.3 (готово) → **3** → 3.4… из [roadmap.md](roadmap.md); параллельно gear23 live (бот, динамический пул).
- Хост: uptime ~15 сут 4 ч (перезагрузка 13.11 05:15 МСК). `spread-collector-next` (PID 917, active с 05:15 МСК 13.11, рестартов нет, 204 пары в heartbeat, failures=0), таймеры compactor/backup/discovery/top10/ops-metrics/pool-dyn-append активны. Окно топ-10 за 24ч: слоты 287/289.
- Юнит `spread-collector-next`, код `/root/spread_staging`, данные `/data/live`. `/root/spread_staging` не git-checkout, SHA нет: крутится задеплоенное дерево. Деплоев кода за сутки не видно (изменён только hot_add_delta.csv). Старый `spread-collector` inactive/disabled.
- gear23 live (зона бота): PID 1222651, запущен 23.11 17:38 МСК (аптайм ~4 сут 16 ч), рестартов нет. Юнит `spread-bbot.service` (старый stub) disabled. Таймер `spread-canary-pool-dyn-append` идёт через drop-in `pool11.conf` (скрипт `/root/pool12/dyn/canary_pool_dyn11_append.py`, откат: удалить drop-in и daemon-reload).
- Backup (rclone в `backup1tb:spread-compacted`) по факту доставки: **D по backup зелёный** — за 24ч 288 ok / 0 fail (норма ~9), неотправленных `*.parquet` в корне `/data/compacted` 0, последний успешный transfer 28.11 09:32 МСК.
- Ночной `spread-bbot-gear23-metrics-compact` 28.11 03:16 МСК: Result=success; `systemctl --failed` пуст.
- Топ-10 volatile 28.11 отработал 09:10 МСК (слоты 287/289): BLUR ME INIT AXIS YB AEON BIGTIME CORE DNA SPACE.
- Соседи на хосте (не веду): `spread-bbot-would-send-prod` inactive/disabled; θ K=1 canary inactive/disabled, oneshot не запускал; gear23 live работает.
- Ops сейчас: load ~0.3, RAM занято ~2.2 из 16 GiB (остальное page cache). Диск 39/79G (51%), свободно ~38G. Данные материализуются в `/data/live`, затем compact в `/data/compacted`, копия — `backup1tb:spread-compacted`.
- Слежу я (голова), утренний дайджест ~9:32 МСК. @бот — Contour B / gear23 live, @стратег — would_send. Sentry ops отдельно, не эта секция.
- Не поднимать старый collector. Чужие data root не чищу без явного OK. Утренний ops-снимок NOW.md коммичу сразу в main. Прочий патч в NOW.md и на сервер — только после явного «да».
