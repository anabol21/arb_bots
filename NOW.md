# NOW

Снимок на 2026-11-24 ~09:45 МСК. Каждая секция — зона своего агента, чужие секции не переписывать.

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

- gear23 / B2.3 live canary, pool11 dyn-лаунчер, обёртка PID 1222650, бот 1222651, запущен 23.11 17:38 МСК (аптайм ~17 ч на 24.11 10:42); рестарт 23.11 осознанный (переход на pool11), halts нет, ERROR/Traceback за 23.11 нет.
- Пул 18 из 60 (база 7: INIT, BARD, IRYS, YB, BREV, SOPH, HUMA + 11 extras: AXIS, BTW, CP, KII seed и 24.11 10:10 добавлены SAHARA, AEON, DOS, ACU, ENSO, SAPIEN, DNA); WAL, LA, RVN вне базы. 1x подтверждён у всех 7 новых; у монет базы/seed подтверждение в логе не проверялось.
- Позиций нет (heartbeat position=None на 10:42 МСК).
- completed_cycles 1 после рестарта (до рестарта 50).
- 23.11: 5 сделок (все INIT long), net $-0.2918 (среднее $-0.0584); funding итого -0.419$ (OKX +0.081, Bybit -0.500), съел Bybit; funding Bybit сходится с closedPnl и private WS. insufficient_size 8, send_abort 0, обрывов private WS 4 (рядом со сделками нет).
- Мониторинг: утренний дайджест ~10:30 МСК, эта секция обновляется после дайджеста.
- size_check только по L1. REST бирж только для разовой сверки funding/PnL.

## Ops / collector (голова)

Сервер, утренний снимок, скрипты юнитов. Живые ордера и would_send не веду. Архитектура: `docs/`.

- Вектор: держать D зелёным (collector/compact/backup) как базу под would_send canary → 2.2 → 2.3 (готово) → **3** → 3.4… из [roadmap.md](roadmap.md); параллельно gear23 live (бот, динамический пул).
- Хост: uptime ~11 сут 4 ч (перезагрузка 13.11 05:15 МСК). `spread-collector-next` (PID 917, active с 05:15 МСК 13.11, рестартов нет, 204 пары в heartbeat, failures=0), таймеры compactor/backup/discovery/top10/ops-metrics/pool-dyn-append активны. Окно топ-10 за 24ч: 203 монеты в рейтинге.
- Юнит `spread-collector-next`, код `/root/spread_staging`, данные `/data/live`. `/root/spread_staging` не git-checkout, SHA нет: крутится задеплоенное дерево. Деплоев кода за сутки не видно. Старый `spread-collector` inactive/disabled.
- gear23 live (зона бота): PID 1222651, запущен 23.11 14:38 МСК (аптайм ~19 ч), heartbeat: 11 монет в пуле (INIT, BARD, IRYS, YB, BREV, SOPH, HUMA, AXIS, BTW, CP, KII), position=None. Юнит `spread-bbot.service` (старый stub) disabled. Таймер `spread-canary-pool-dyn-append` с 23.11 14:38 МСК идёт через drop-in `pool11.conf` (скрипт `/root/pool12/dyn/canary_pool_dyn11_append.py`, откат: удалить drop-in и daemon-reload).
- Backup (rclone в `backup1tb:spread-compacted`) по факту доставки: **D по backup зелёный** — за 24ч 288 ok / 6 fail попыток (4 уникальных файла, норма ~9; все позже доставлены): copyto watchdog 195–265 с, 23.11 11:47–12:01 и 22:03–22:14 МСК; неотправленных `*.parquet` в корне `/data/compacted` 1 (свежий, ~3 мин), последний успешный transfer 24.11 09:31 МСК.
- Ночной `spread-bbot-gear23-metrics-compact` 24.11 03:15 МСК: Result=success; `systemctl --failed` пуст.
- Топ-10 volatile 24.11 отработал 09:10 МСК: INIT SAHARA AEON DOS ACU ENSO SAPIEN CP KII DNA.
- Соседи на хосте (не веду): `spread-bbot-would-send-prod` inactive/disabled; θ K=1 canary inactive/disabled, oneshot не запускал; gear23 live работает. HL v2 не запущен.
- Ops 24ч: CPU ср. 5.5% / пик 25%, load сейчас ~0.2, RAM занято ср. 2.6 / пик 5.1 из 16 GiB, egress ~7.2 GiB tx. Диск 39/79G (52%), свободно ~37G. Данные материализуются в `/data/live`, затем compact в `/data/compacted`, копия — `backup1tb:spread-compacted`.
- Слежу я (голова), утренний дайджест ~9:32 МСК. @бот — Contour B / gear23 live, @стратег — would_send. Sentry ops отдельно, не эта секция.
- Не поднимать старый collector. Чужие data root не чищу без явного OK. Утренний ops-снимок NOW.md коммичу сразу в main. Прочий патч в NOW.md и на сервер — только после явного «да».
