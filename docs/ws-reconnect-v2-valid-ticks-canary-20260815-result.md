# Valid-ticks canary — result 2026-08-16 (pre-B)

Трек: сбор. Блок: WS lifecycle + fail-closed запись тиков.
**Не Track B.** Инвариант: дыра лучше, чем stale-cross тик.

| Поле | Значение |
|------|----------|
| Окно | 2026-08-15 17:30:12 UTC → stop 2026-08-16 **17:32:36 UTC** (**24 ч 2 мин**) |
| Subscribe-complete | 2026-08-15 17:32:43 UTC |
| Процесс | PID **530649**, `NRestarts=0`, затем `systemctl stop` + **disabled** |
| Вердикт | **canary hold** |
| B | не READY — следующий шаг: latency-профиль / конфиг (pre-B) |

Дашборд: [valid-ticks canary](/Users/mishatrubik/.cursor/projects/Users-mishatrubik-Desktop-spread/canvases/ws-reconnect-v2-valid-ticks-canary.canvas.tsx)

## Stop

- `systemctl stop spread-collector` 17:32:31–17:32:36 UTC.
- `shutdown_flush_done | published_files=225444 | published_rows=66977601 | bytes_written=3750418901 | published_jobs=670`
- `failures=0`, spool=0 на последнем heartbeat.
- Unit **disabled** (не поднимется после reboot) — хост свободен для latency-экспериментов.
- `spread-compactor.timer` и `spread-backup-transfer.timer` **оставлены** active/enabled, чтобы добить live→compact→remote ticks.

## Счётчики (последний heartbeat 17:31:48Z + flush)

- `ws_disconnect` / planned = **178 / 178**; unplanned=0; protocol=0; unrecovered=0
- close все **1006**; abrupt 160, keepalive 18
- Bybit `orderbook.1` 170 / OKX books5 4 / OKX candle5m 4
- `ws_budget_exceeded` = **0**
- Bybit `wave_60s` max **40** (наблюдение, не gate); OKX max **2**; кластер 15-е 19–21 UTC
- ingest: **67.0M** tick rows, **96.5k** bars (heartbeat), failures=0
- suppress: stale **13.57M**, generation **1126**, accepted **66.95M**
- parquet sample 80 файлов / 6857 строк: skew_violations=0, age_violations=0; max 1998 / 1999 ms
- coverage: 178 incomplete окон = 178 disconnect, 141 монет
- journal OOM по collector/compactor/backup за окно: **0**
- диск 56 GiB free; bars локально **1.5 GiB**

## Gates

| Gate | Результат |
|------|-----------|
| Процесс / v2 | pass |
| Unplanned / protocol / unrecovered | pass |
| Fail-closed parquet | pass (sample) |
| Generation в волне disconnect | pass (631→1123 за 19:41–21:31Z) |
| Incomplete = disconnect | pass |
| Ingest ticks+bars, failures=0 | pass |
| Wave ≤8 | **не gate** (факт Bybit 40) |

## Политика bars (GD 2026-08-16)

Durable bars = **VPS-local** `/data/bars` (~1.5 GiB). Remote bars-backup **не** требуется для pre-B. Durable ticks по-прежнему `backup1tb:spread-compacted`.

## Что это закрывает / не закрывает

Закрыто на 1-WS-на-пару: planned reconnect, fail-closed тики, connect scheduler, честные дыры.

Не закрыто: контракт задержек (p99 / S/P / dual), multiplexer, Track B / live.

Вернуть сбор: `systemctl enable --now spread-collector` (drop-in reconnect-v2 уже на месте).
