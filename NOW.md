# NOW

Снимок на 2026-10-07 ~09:40 МСК. Каждая секция — зона своего агента, чужие секции не переписывать.

Вектор развития: [roadmap.md](roadmap.md). Этот файл — снимок, не план.

## would_send (стратег)

Симуляция гира 2.2, `would_send`, `send=false`. Живых ордеров нет.

- Юнит `spread-bbot-would-send-prod`, данные `/data/bbot-would-send-prod`.
- SHA checkout на хосте `8af4d52` (код #75 `BBOT_SIZE_GATE=0`; tip main).
- Статус 07.10 ~00:39 МСК: выкатили `8af4d52` (detached) поверх `c3d3e5f`, рестарт только `spread-bbot-would-send-prod` на flat book (`position=None` / orphan θ нет). Size gate на stub **выключен**: unit env `BBOT_SIZE_GATE=0` — `size_check` всё ещё пишет available/planned (`size_ok_raw`), но `size_ok=true` и не блокирует open/close. Live/gear23 не трогали (default gate on). `BBOT_NOTIONAL_USDT=20`, AGE/SKEW 10s (`SPREAD_TICK_AGE_MAX_MS=10000` / `SPREAD_TICK_SKEW_MAX_MS=10000`). Ротация expand-only в 10:00 МСК. Пул 54 = база 29 + 25 экстра (AEON, API3, ARX, BREV, CT, DGAI, ENJ, ESP, GMX, KGEN, LAB, LQTY, MANA, OPN, RECALL, RESOLV, RSR, SAND, SENT, SPACE, TRIA, TRUTH, UMA, WOO, YGG). Открытых θ нет. Канарейка theta-k1 остановлена. После деплоя: `insufficient_size` на stub не должен резать сделки; смотреть opens/heartbeat.
- Гейты: θ_open 0.50, p50_open 0.60, min_profit 0.20, fee 0.30, K=1; tick AGE/SKEW 10000 мс; **size gate off** (`BBOT_SIZE_GATE=0`).
- Следит Sentry (issue created) на открытиях.
- Не трогать Contour B и `/data/live`. Шов рестарта: hot-add экстра прогревается из устаревшей history, floor прыгает.

## Ops / collector (голова)

Сервер, утренний снимок, скрипты юнитов. Живые ордера и would_send не веду. Архитектура: `docs/`.

- Вектор: держать D зелёным (collector/compact/backup) как базу под would_send canary и 2.2 из [roadmap.md](roadmap.md); параллельно крутятся would_send-prod (стратег) и HL v2 (Старшой). Contour B live canary inactive — не заброс B. 2.5/3 / полный пул / bars on / live без команды — не трогаем.
- Юнит `spread-collector-next`, код `/root/spread_staging`, данные `/data/live`.
- SHA нет: `/root/spread_staging` не git-checkout, крутится задеплоенное дерево (последний деплой — cutover 22.09).
- Статус 07.10 ~09:38 МСК: active с 22.09 14:07 МСК, NRestarts=0, MemoryCurrent ~1.4G (MemoryMax infinity), pairs=198, ws_subscribe_ok=7835, ws_reconnect_unplanned=1, collect_bars=false, failures=0, rejected=0, свежих файлов в `/data/live` за 5 мин ~590. Топ-10 volatile (OKX–Bybit, rolling 24ч) отработал 07.10 09:10–09:16 МСК (198 монет, слоты 287/289). θ-compact 03:15 МСК: 0 дней, 76 skip, 0 ошибок. Compactor/backup идут, backlog 0. Таймеры compact/backup/discovery/ops-metrics/top10 active.
- Деплои за сутки (по юнитам на хосте): `spread-bbot-would-send-prod` перезапущен 07.10 00:38 МСК на `8af4d52` (= tip кода main, #75 `BBOT_SIZE_GATE=0`). Ручной прогон gear23-live из `/root/b-private-b-exp` 06.10 16:54–17:07 МСК остановился на `private_or_synthetic_warm_failed` (signal loop не стартовал), юнита нет. HL v2 с 04.10 16:43 МСК, collector-next с 22.09 — без изменений.
- Соседи на хосте (не веду): `spread-bbot-would-send-prod` active с 07.10 00:38 МСК, MemoryMax 2G / ~0.25G, SHA `8af4d52`, пул 54 монеты (база 29 + 25 экстра), `position=None`; `spread-collector-hl-v2` active с 04.10 16:43 МСК, MemoryMax 6G / ~1.1G, heartbeat bybit/okx/hl 198/198/88, dropped=0. `spread-bbot-gear22-live-canary`, `gear2` и `theta-k1` inactive/disabled. Старые failed-юниты (bars-compactor/backup с 13.08, ev2-10/11/12 с 21–23.09) — давний хвост, не новое.
- Лимиты collector: skew/age 2000 мс, HOT_ADD max_extra=8, bars off. Диск 47/79G, свободно ~29G (`/data/live` 2.2G, `live_hl_v2` ~0.68G, `bbot-would-send-prod` 17G и растёт ~4G/сутки — при таком темпе ~7 суток до заполнения, `compacted` 3.9G). Ops 24ч: CPU avg 27% / peak 69%, load ~2.0, RAM 2.4/16G (peak 3.8G), egress 13.9 GiB / peak 9.0 Mbit/s.
- Слежу я (голова), утренний дайджест ~9:32 МСК. @бот — Contour B (молчит), @стратег — would_send, @Старшой — HL. Sentry ops отдельно, не эта секция.
- Не поднимать старый collector. Чужие data root не чищу без явного OK. Утренний ops-снимок NOW.md коммичу сразу в main. Прочий патч в NOW.md и на сервер — только после явного «да».

## HL v2 (Старшой)

Изолированный контур сбора Bybit/OKX + Hyperliquid. Не prod writer и не Contour B.

- Юнит `spread-collector-hl-v2`, код `/root/spread_hl_v2`, данные `/data/live_hl_v2`. Бэкап `backup1tb:spread-hl-v2` из `/data/compacted_hl_v2`.
- SHA checkout на хосте `4a76890` (ветка `main_hl`, dirty: host-патчи flush, tick-gate, reconnect и universe), не tip main.
- Статус 03.10: зелёная, heartbeat 198/198/88. Обрыв HL WS ~12:10–13:56 МСК сам поднялся.
- Лимиты: MemoryMax 6G, disk-guard 15 GiB, CPUQuota 200%. Flush 100000/45 с, skew/age 2000 мс, компактор 2 мин / 2500M. Один HL-сокет на 88 монет. Реконнект HL = 100, Bybit/OKX остаются на 2.
- Слежу я, часовой watch только на red.
- Не трогать prod `/data/live` и Contour B. CEX-пул не сужать. При scale-up перестраивать knobs до запуска.
