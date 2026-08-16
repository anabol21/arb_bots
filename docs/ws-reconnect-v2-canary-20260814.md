# WS reconnect v2 canary — 2026-08-14 (pre-B)

Трек: сбор. Блок: WebSocket **lifecycle only** (parse/spread frozen).
Это **не** Track B и не live-bot. Цель — на текущей модели 1-WS-на-пару
заменить `error + sleep 10s` на **planned reconnect** и вечером снять
evidence для pre-B.

| Поле | Значение |
|------|----------|
| Хост | `root@38.180.94.108` |
| Код | локальный репо → `/root/spread_staging` |
| Entrypoint | `app/screaner_b_o.py` |
| Флаг | `SPREAD_WS_RECONNECT_V2=1` (drop-in; в git unit default остаётся `0`) |
| Профиль | lean+bars, N≈337, как до остановки |
| Лог | `/var/log/spread/runtime.log` |
| Старт canary | **2026-08-14 12:30:59 UTC** (`reconnect_mode=v2`, PID 445090) |
| Вечерний срез | тот же день, окно ≥4–6 ч после subscribe-complete |

Collector был остановлен ~12:20 UTC для деплоя. Между stop и start — явный
gap в ticks/bars; это не reconnect-баг.

## Что должно быть в логе

- `reconnect_mode=v2` в `runtime_paths` и heartbeat
- события `ws_disconnect`, `ws_reconnect_planned`, `ws_subscribe_ok`
- `ws_reconnect_unplanned` только для `protocol_error` (budget 0)
- heartbeat: `ws_wave_*_60s`, `ws_unrecovered_active`, `ws_budget_exceeded`

Не считать победой «меньше ERROR-строк». Считать классификацию. `ws_wave_*`
остаётся в логе как размер пачки drop, не как pass/fail.

## Вечерние gates (canary, не приёмка B)

| Gate | Pass |
|------|------|
| Процесс | `spread-collector` active, `NRestarts=0` |
| Режим | heartbeat `reconnect_mode=v2` |
| Unplanned | `ws_reconnect_unplanned=0` и `ws_protocol_errors=0` за окно |
| Unrecovered | `ws_unrecovered_active=0` к срезу |
| Ingest | heartbeat `published_rows` растёт; `collect_bars=true`; нет новой дыры bars после subscribe-complete |
| Старый путь | нет массовых `OKX error` / `Bybit error` / `candle5m error` + sleep 10s |

**Поправка 2026-08-16:** строка Wave (`ws_wave_*_60s` ≤8) снята с canary и с
требований Track B. Кластер planned disconnect не блокирует B.

Budget ≤2 planned reconnect / соединение / 60 мин — наблюдаем; первое
превышение логируется `reconnect_budget_exceeded`, не silent tight loop.

## Что это даёт pre-B

Контракт (`docs/latency-production-acceptance-contract.md`): reconnect
разрешён только как именованное planned событие. Fan-out multiplexer в
прод не тащим, пока этот canary зелёный на 1-WS-на-пару.

Вечерний отчёт должен содержать: окно UTC, counts planned/unplanned/
unrecovered, NRestarts, wave как наблюдение, и вердикт `canary hold / fail` —
не `B READY`.
