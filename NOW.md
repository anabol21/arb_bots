# NOW

Снимок на 2026-10-06 ~09:40 МСК. Каждая секция — зона своего агента, чужие секции не переписывать.

Вектор развития: [roadmap.md](roadmap.md). Этот файл — снимок, не план.

## would_send (стратег)

Симуляция гира 2.2, `would_send`, `send=false`. Живых ордеров нет.

- Юнит `spread-bbot-would-send-prod`, данные `/data/bbot-would-send-prod`.
- SHA checkout на хосте `3f9a1df` (код #71; tip main сдвинулся, контур не перекатывался под новый tip).
- Статус 06.10 ~22:00 МСК: крутится после рестарта на flat book (orphan нет). AGE/SKEW freshness подняты 2s→10s через unit env `SPREAD_TICK_AGE_MAX_MS=10000` / `SPREAD_TICK_SKEW_MAX_MS=10000` (бэкап юнита `*.bak.20261006-age10s`), код/SHA без смены. Ротация expand-only в 10:00 МСК. Пул 54 = база 29 + 25 экстра (AEON, API3, ARX, BREV, CT, DGAI, ENJ, ESP, GMX, KGEN, LAB, LQTY, MANA, OPN, RECALL, RESOLV, RSR, SAND, SENT, SPACE, TRIA, TRUTH, UMA, WOO, YGG). Открытых θ нет. Канарейка theta-k1 остановлена.
- Гейты: θ_open 0.50, p50_open 0.60, min_profit 0.20, fee 0.30, K=1; tick AGE/SKEW 10000 мс (unit env).
- Следит Sentry (issue created) на открытиях.
- Не трогать Contour B и `/data/live`. Шов рестарта: hot-add экстра прогревается из устаревшей history, floor прыгает.

## Ops / collector (голова)

Сервер, утренний снимок, скрипты юнитов. Живые ордера и would_send не веду. Архитектура: `docs/`.

- Вектор: держать D зелёным (collector/compact/backup) как базу под would_send canary и 2.2 из [roadmap.md](roadmap.md); параллельно крутятся would_send-prod (стратег) и HL v2 (Старшой). Contour B live canary inactive — не заброс B. 2.5/3 / полный пул / bars on / live без команды — не трогаем.
- Юнит `spread-collector-next`, код `/root/spread_staging`, данные `/data/live`.
- SHA нет: `/root/spread_staging` не git-checkout, крутится задеплоенное дерево (последний деплой — cutover 22.09).
- Статус 06.10 ~09:36 МСК: active с 22.09 14:07 МСК, NRestarts=0, MemoryCurrent ~1.8G (MemoryMax infinity), pairs=198, ws_subscribe_ok=7525, ws_reconnect_unplanned=1, collect_bars=false, failures=0, rejected=0, свежих файлов в `/data/live` за 5 мин ~760. Топ-10 volatile (OKX–Bybit, rolling 24ч) отработал 06.10 09:10–09:15 МСК (198 монет, слоты 287/289). θ-compact 03:15 МСК: 0 дней, 76 skip, 0 ошибок. Таймеры compact/backup/discovery/ops-metrics/top10 active.
- Деплои за сутки (по юнитам на хосте): нет. would_send-prod с 04.10 14:31 МСК на `3f9a1df` (= код-tip main), HL v2 с 04.10 16:43 МСК, collector-next с 22.09. Ветки `preB2.2` и PR #72/#73 (Sentry на `terminal_private`) на хост не выкатывались, gear22-live-canary inactive.
- Соседи на хосте (не веду): `spread-bbot-would-send-prod` active с 04.10 14:31 МСК, MemoryMax 2G / ~0.25G, SHA `3f9a1df`, пул 50 монет (база 29 + 21 экстра, ротация 05.10 добавила RSR/GMX/TRIA/BREV/SPACE); `spread-collector-hl-v2` active с 04.10 16:43 МСК, MemoryMax 6G / ~1.0G, heartbeat bybit/okx/hl 198/198/88, dropped=0, reconn=0. `spread-bbot-gear22-live-canary` и `theta-k1` inactive/disabled.
- Лимиты collector: skew/age 2000 мс, HOT_ADD max_extra=8, bars off. Диск 41/79G, свободно ~36G (`/data/live` 1.8G, `live_hl_v2` ~0.64G, `bbot-would-send-prod` 13G и растёт ~3.5G/сутки, `compacted` 3.7G). Ops 24ч: CPU avg 21% / peak 52%, RAM 2.0/16G, egress 11.9 GiB / peak 4.5 Mbit/s.
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
