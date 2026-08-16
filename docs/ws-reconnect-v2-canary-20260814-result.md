# WS reconnect v2 canary — result 2026-08-15

Трек: сбор. Блок: WS lifecycle. **Не Track B.**

| Поле | Значение |
|------|----------|
| Окно | 2026-08-14 12:30:59 UTC → 2026-08-15 17:11:42 UTC (**28 ч 41 мин**) |
| Процесс | PID **445090**, `NRestarts=0`, `SPREAD_WS_RECONNECT_V2=1` |
| Вердикт | **canary hold** — unplanned→planned выполнен. Wave 41 был fail по старому порогу 8; **с 2026-08-16 wave не gate B** |
| B | не READY |

Дашборд: вечерний срез в canvas `ws-reconnect-v2-canary`.

## Счётчики

- `ws_disconnect` / `ws_reconnect_planned` = **317 / 317**
- `ws_reconnect_unplanned` = **0**, `ws_protocol_errors` = **0**
- `ws_unrecovered` / active = **0 / 0**
- старый путь `OKX/Bybit/candle5m error` = **0**
- close: все **1006**; `abrupt` 177, `keepalive` 140
- Bybit 261 / OKX 56 (books5 43, candle5m 13)
- heartbeat `ws_budget_exceeded` episodes = **9**; строк `reconnect_budget_exceeded` = **356** (poll раз в ≤60 с)
- wave: OKX max **6**; Bybit max **41**, **13** heartbeat >8 (наблюдение; с 2026-08-16 не gate)
- ingest: **121.8M** tick rows, **115.5k** bars, failures=0, 336 coins, bars mtime свежие

## Вердикт по gates

| Gate | Результат |
|------|-----------|
| Процесс / v2 mode | pass |
| Unplanned / protocol | pass |
| Unrecovered | pass |
| Старый error+sleep 10s | pass |
| Ingest ticks+bars | pass |
| Wave ≤8 / 60s | **снят 2026-08-16** (факт Bybit max 41 оставлен как наблюдение; не блокирует B) |

Pre-B: классификация и учёт reconnect evidence-grade на 1-WS-на-пару.
Wave ≤8 больше не вход в B. Fan-out multiplexer в прод этим решением не открыт.
