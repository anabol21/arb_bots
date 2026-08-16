# L1 live — `l1_n337_write_20260816`

> ## ВЛАДЕЛЕЦ / OWNER — Track (D) latency
>
> Окно 1 **завершено** (`ping_exit=0` в `19:05:27Z`). Сборщик PID 607951
> ещё `active` — это **не** второе окно. Артефакты
> `/data/experiments/l1_n337_write_20260816` только читать.
> Итог: [результаты](latency-l1-results.md).

| Поле | Значение |
|------|----------|
| Трек | (D) latency; не B |
| Профиль | L1: N=337 lean+bars, тики backup, бары VPS-local, reconnect v2 |
| Хост | `root@38.180.94.108` |
| Entrypoint | `app/screaner_b_o.py` SHA `c06d677aa036dddc…` (тот же, что canary 15.08) |
| Collector | `spread-collector.service` PID **607951**, `NRestarts=0`, enabled |
| Drop-in | `SPREAD_WS_RECONNECT_V2=1`, `CONNECT_PER_SEC=3`, skew/age 2000 |
| Heartbeat | `pairs=337`, `schema_mode=lean`, `collect_bars=true`, `reconnect_mode=v2`, `failures=0` |
| Ping unit | `latency-l1-20260816.service` PID **608421** |
| Ping | `validation/ws_fanout_matched_ping.py` XRP, 4200 s |
| Collector start | `2026-08-16T17:51:18Z` |
| Subscribe-ready | `2026-08-16T17:55:27Z` (`ws_subscribe_ok=1008`) |
| Ping start | `2026-08-16T17:55:27Z` |
| Warmup end | `2026-08-16T18:05:27Z` |
| Steady end / ping end | `2026-08-16T19:05:27Z` |
| Experiment root | `/data/experiments/l1_n337_write_20260816/` |
| Marker | `/var/log/spread/DO_NOT_TOUCH_LATENCY_L1_20260816.txt` |
| Фон | `spread-compactor.timer` и `spread-backup-transfer.timer` active |
| Дизайн | [лестница](latency-profile-ladder-design.md) |
| Контракт | [приёмка](latency-production-acceptance-contract.md) |

## Инцидент на старте

Первый ping в `17:51:19Z` стартовал по **старому** heartbeat canary
(`ws_subscribe_ok=1189` ещё в том же `runtime.log`). Сборщик не
перезапускали. Premature ping остановлен в `17:52:09Z`; лог сохранён как
`ping_xrp.premature.jsonl` и **не** входит в расчёт. Рабочий ping начат
после `ws_subscribe_ok=1008` у текущего процесса.

## Gates этого окна

Reject, если в steady `18:05:27Z–19:05:27Z`:

- pooled S p99 ≥ 100 ms на OKX или Bybit; или
- S/P p99 > 2.0× на любой ноге; или
- dual>500 более 1 минуты из 60; или dual>1000 > 0; или
- unplanned / protocol / unrecovered > 0; или
- backpressure / publish overflow > 0; или
- нет overlap ping на весь steady.

Accept этого окна недостаточно для фиксации конфига: нужно второе
независимое 60-мин окно.

## Что не меняем

Ingest, parse, spread, writer, compactor, backup. Только enable текущего
unit и отдельный ping.
