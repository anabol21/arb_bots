# Valid-ticks canary — early cut 2026-08-16 15:51 UTC (pre-B)

Трек: сбор. Блок: WS lifecycle + fail-closed запись.
**Не Track B.** Окно **22.35 ч** после старта (subscribe-complete 17:32:43 UTC 15-го) — до суток ~1.6 ч.

| Поле | Значение |
|------|----------|
| Старт | 2026-08-15 17:30:12 UTC, PID **530649**, `NRestarts=0` |
| Срез | 2026-08-16 15:51:17 UTC |
| Режим | `reconnect_mode=v2`, `connect_per_sec=3.0`, skew/age 2000 ms |
| Вердикт | **canary hold (interim)** — unplanned/fail-closed зелёные. Wave не gate B с 2026-08-16 |
| B | не READY |

Дашборд: [valid-ticks canary](/Users/mishatrubik/.cursor/projects/Users-mishatrubik-Desktop-spread/canvases/ws-reconnect-v2-valid-ticks-canary.canvas.tsx)

## Счётчики

- `ws_disconnect` / planned = **178 / 178**; unplanned=0; protocol=0; unrecovered=0
- close все **1006**; abrupt 160, keepalive 18
- Bybit `orderbook.1` 170 / OKX books5 4 / OKX candle5m 4
- `ws_budget_exceeded` = **0** (в прошлом canary было 9 эпизодов)
- Bybit `wave_60s` max **40**, 13 heartbeat >8; OKX max **2**
- кластер тот же вечерний слот: 15-е **19–21 UTC** (55+103+15 disconnect)
- ingest: **61.6M** tick rows, **90k** bars, failures=0
- suppress: stale **12.65M**, generation **1126**, accepted **61.6M**
- parquet sample (80 свежих файлов, 6869 строк): skew_violations=0, age_violations=0; max 1998 / 1999 ms

## Gates

| Gate | Результат |
|------|-----------|
| Процесс / v2 | pass |
| Unplanned / protocol / unrecovered | pass |
| Fail-closed (skew/age в parquet ≤2000) | pass (sample) |
| Generation suppress в минуты волны | pass (631→1123 за 19:41–21:31Z) |
| Incomplete окна = disconnect | pass (178 = 178) |
| Wave ≤8 / 60s | **снят 2026-08-16** (факт Bybit max 40 — наблюдение, не блок B) |
| Bars живые, дыры ок | pass на этом срезе |

Планировщик connect не убирает WAVE-счётчик (это drop за 60 с). Стадо reconnect
не крутит budget (0). С 2026-08-16 wave не требование B.

До полного ≥24 ч оставить процесс. Не рестартить. Не B READY.

Финальный отчёт: [ws-reconnect-v2-valid-ticks-canary-20260815-result.md](ws-reconnect-v2-valid-ticks-canary-20260815-result.md) (stop 2026-08-16 17:32:36 UTC).
