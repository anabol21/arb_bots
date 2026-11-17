# NOW

Снимок на 2026-11-17 ~09:45 МСК. Каждая секция — зона своего агента, чужие секции не переписывать.

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

- gear23 / B2.3 live canary, PID 69299, запущен 13.11 18:58 МСК, аптайм ~88 ч на 17.11 10:45; рестартов и halts нет, ERROR/Traceback за 16.11 нет (private WS: 6 обрывов, 4 неудачных auth, все с переподключением, рядом со сделками нет).
- Пул 52 из 60 (3 базовые WAL, RVN, LA + 49 extras); 14.11 сайдкар добавил BERA, SHELL (1x подтверждён); 15.11, 16.11 и 17.11 10:10 МСК ротация без добавлений (топ-10 в пуле, AKE и JCT пропущены: no_complete_meta). Нет подтверждения 1x в логе у APR и BLEND.
- Открыта INIT long b0530df7 с 17.11 07:05 МСК (heartbeat 07:44 МСК).
- completed_cycles 20 на конец 16.11 (было 9).
- 16.11: 11 сделок (SOPH×2, INIT×7, AXIS, HUMA), net $+0.0314 по журналу; у INIT 235eb2e4 funding не вошёл в журнал (REST: OKX -0.0452, Bybit -0.0103), с поправкой net ≈ $-0.024; в остальных funding сходится с closedPnl Bybit. insufficient_size 3 (INIT), send_abort 0.
- Мониторинг: утренний дайджест ~10:30 МСК, эта секция обновляется после дайджеста.
- size_check только по L1. REST бирж только для разовой сверки funding/PnL.

## Ops / collector (голова)

Сервер, утренний снимок, скрипты юнитов. Живые ордера и would_send не веду. Архитектура: `docs/`.

- Вектор: держать D зелёным (collector/compact/backup) как базу под would_send canary → 2.2 → 2.3 (готово) → **3** → 3.4… из [roadmap.md](roadmap.md); параллельно gear23 live (бот, динамический пул).
- Хост: uptime ~4 сут 4 ч (перезагрузка 13.11 05:15 МСК). `spread-collector-next` (PID 917, active с 05:15 МСК 13.11, рестартов нет, 204 пары, failures=0), таймеры compactor/backup/discovery/top10/ops-metrics активны. Окно топ-10 за 24ч: 287/289 слотов.
- Юнит `spread-collector-next`, код `/root/spread_staging`, данные `/data/live`. `/root/spread_staging` не git-checkout, SHA нет: крутится задеплоенное дерево. Деплоев кода и изменений unit-файлов за сутки не видно. Старый `spread-collector` inactive/disabled.
- gear23 live (код `gear23-b2.3-tw-deque-20261010-tradeobs-20261018T1741Z`): PID 69299 с 13.11 18:58 МСК (~3.6 сут), `completed_cycles` 21; по canary_state открыта INIT long 10 USDT с 17.11 07:05 МСК. Зона бота. Юнит `spread-bbot.service` (старый stub) сейчас inactive.
- Backup (rclone в `backup1tb:spread-compacted`) по факту доставки: D по backup зелёный — за 24ч 288 ok / 0 fail, неотправленных `*.parquet` в корне `/data/compacted` 1 (свежий, ~1 мин), последний успешный transfer 17.11 09:33 МСК.
- Не зелёное: `spread-bbot-gear23-metrics-compact` упал в ночном прогоне 17.11 03:15 МСК (Result=exit-code, status 1, days_errors=1): `theta_trades/event_date=2026-11-15` verify_mismatch_existing, parquet rows=25 != jsonl kept=49. Юнит в `systemctl --failed`; не трогал, чинит владелец (бот). Compactor по таймеру без ошибок.
- Топ-10 volatile 17.11 отработал 09:10 МСК: INIT JCT AXIS SOPH HUMA CP DNA AEON APR AZTEC.
- Соседи на хосте (не веду): `spread-bbot-would-send-prod` inactive/disabled; θ K=1 canary inactive/disabled, oneshot не запускал; gear23 live работает. HL v2 не запущен.
- Ops 24ч: CPU ср. 6% / пик 25%, load сейчас ~0.3, RAM ср. 1.7 / пик 2.6 из 16 GiB, egress ~7.2 GiB. Диск 38/79G (51%), свободно ~38G. Данные материализуются в `/data/live`, затем compact в `/data/compacted`, копия — `backup1tb:spread-compacted`.
- Слежу я (голова), утренний дайджест ~9:32 МСК. @бот — Contour B / gear23 live, @стратег — would_send. Sentry ops отдельно, не эта секция.
- Не поднимать старый collector. Чужие data root не чищу без явного OK. Утренний ops-снимок NOW.md коммичу сразу в main. Прочий патч в NOW.md и на сервер — только после явного «да».
