# NOW

Снимок на 2026-10-04 ~09:42 МСК. Каждая секция — зона своего агента, чужие секции не переписывать.

## would_send (стратег)

Симуляция гира 2.2, `would_send`, `send=false`. Живых ордеров нет.

- Юнит `spread-bbot-would-send-prod`, данные `/data/bbot-would-send-prod`.
- SHA checkout на хосте `a12d593`, не tip main (в main #64 `426d689` и #68 `3cd1845`, контур не перекатывался).
- Статус 03.10: крутится. Ротация expand-only в 10:00 МСК, дропов нет. Пул 42 = база 29 + 13 экстра (CT, AEON, OPN, ARX, RECALL, LQTY, SAND, MANA, ENJ, RESOLV, SENT, WOO, TRUTH; утром добавлены последние семь, 2Z уже в базе). Открыт SAND long с 12:59:55 МСК (`d0c91dee`). Канарейка theta-k1 остановлена. Рестарт со сбросом сделки не сделан.
- Гейты: θ_open 0.50, p50_open 0.60, min_profit 0.20, fee 0.30, K=1.
- Следит Sentry (issue created) на открытиях.
- Не трогать Contour B и `/data/live`. Шов рестарта: hot-add экстра прогревается из устаревшей history, floor прыгает.

## Ops / collector (голова)

Сервер, утренний снимок, скрипты юнитов. Живые ордера и would_send не веду. Архитектура: `docs/`.

- Вектор: держать D зелёным (collector/compact/backup); параллельно крутятся would_send-prod (стратег) и HL v2 (Старшой). Contour B live canary inactive — не заброс B. 2.5/3 / полный пул / bars on / live без команды — не трогаем.
- Юнит `spread-collector-next`, код `/root/spread_staging`, данные `/data/live`.
- SHA нет: `/root/spread_staging` не git-checkout, крутится задеплоенное дерево.
- Статус 04.10 ~09:42 МСК: active с 22.09 14:07 МСК, NRestarts=0, MemoryCurrent ~1.5G (MemoryMax infinity), pairs=198, collect_bars=false, failures=0, unrecovered=0. Топ-10 volatile (OKX–Bybit, rolling 24ч) отработал 04.10 09:10–09:15 МСК. Таймеры compact/backup/discovery/ops-metrics/top10 active.
- Соседи на хосте (не веду): `spread-bbot-would-send-prod` active с 02.10 14:38 МСК, MemoryMax 2G / ~229M, CPUQuota 100%, SHA `a12d593`, пул 42, position=None после close SAND; `spread-collector-hl-v2` active с 03.10 14:11 МСК, MemoryMax 6G / ~831M, CPUQuota 200%, heartbeat bybit/okx/hl 198/198/88. `spread-bbot-gear22-live-canary` и `theta-k1` inactive.
- Лимиты collector: skew/age 2000 мс, HOT_ADD max_extra=8, bars off. Диск 32/79G, свободно ~45G (`/data/live` 1.7G, `live_hl_v2` ~0.6G, `bbot-would-send-prod` 6.3G, `compacted` 3.6G).
- Слежу я (голова), утренний дайджест ~9:32 МСК. @бот — Contour B (молчит), @стратег — would_send, @Старшой — HL. Sentry ops отдельно, не эта секция.
- Не поднимать старый collector. Чужие data root не чищу без явного OK. Патч в NOW.md — только после явного «да».

## HL v2 (Старшой)

Изолированный контур сбора Bybit/OKX + Hyperliquid. Не prod writer и не Contour B.

- Юнит `spread-collector-hl-v2`, код `/root/spread_hl_v2`, данные `/data/live_hl_v2`. Бэкап `backup1tb:spread-hl-v2` из `/data/compacted_hl_v2`.
- SHA checkout на хосте `4a76890` (ветка `main_hl`, dirty: host-патчи flush, tick-gate, reconnect и universe), не tip main.
- Статус 03.10: зелёная, heartbeat 198/198/88. Обрыв HL WS ~12:10–13:56 МСК сам поднялся.
- Лимиты: MemoryMax 6G, disk-guard 15 GiB, CPUQuota 200%. Flush 100000/45 с, skew/age 2000 мс, компактор 2 мин / 2500M. Один HL-сокет на 88 монет. Реконнект HL = 100, Bybit/OKX остаются на 2.
- Слежу я, часовой watch только на red.
- Не трогать prod `/data/live` и Contour B. CEX-пул не сужать. При scale-up перестраивать knobs до запуска.
