# Статус дата-пайплайна — 2026-08-14

Трек: сбор и хранение. Хост: VPS `root@38.180.94.108` (16 GiB / 80 GiB).
Entrypoint: `app/screaner_b_o.py` (`SPREAD_LEAN_SCHEMA=1`, `SPREAD_COLLECT_BARS=1`, N≈337).

Это снимок того, что уже есть в коде и что доказано на VPS. Не является
разрешением Track B и не утверждает unattended ticks+bars READY.

## Что есть

- Lean ticks + локальные OKX `bar_5m`.
- Tick pipeline: live → compact (`--max-windows 1`, streaming) →
  `backup1tb:spread-compacted` → sent/archive retention.
- Bars: локальный hive `/data/bars/bar_5m`. Compacted-bars v2
  (`/data/bars_compacted_v2`, remote `backup1tb:spread-bars-compacted-v2`)
  реализован, isolated SHA-smoke прошёл, **recurring timers выключены**.
- Systemd: collector, tick compact/backup; ops alerts; runbooks.

## Что работает хорошо

- Collector жив с 2026-08-10 22:54 UTC, `NRestarts=0`.
- Ticks: 8h canary без OOM/TERM; compact lag 1–7 мин; remote растёт;
  pending=0. Durable ticks ≈2529 окон / 29 GiB (3–13 авг). Полные lean-дни
  6–9 и 11–12 авг; 13-е непрерывно до среза. Дыры: 4–5 авг (canary) и
  10 авг (ENOSPC/миграция).
- Локальные bars пишутся: 336 монет, партиции 11–14 авг, heartbeat
  `collect_bars=true`. Это **не** remote bars.

## Что доработать (следующий приоритет: WS reconnect)

1. **Planned reconnect** в collector: сейчас любой обрыв → `error` + sleep 10s
   + connect. Для B нужны классификация close, backoff+jitter, wave gate,
   budget, structured `ws_*` события. Ingest freeze снять только на lifecycle.
2. **Bars durable**: включить v2 timers после изоляции leftover source;
   90 мин rate proof; overnight ticks+bars. Tiny-file backup (~751/ч) не
   догоняет ingest (~4000/ч).
3. Наблюдаемость reconnect в heartbeat; не смешивать v1 и lean в модели.

## Явно не READY

Полный unattended ticks+bars; remote bars freshness; Track B / live bot.
