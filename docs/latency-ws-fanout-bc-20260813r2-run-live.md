# Live record — `wsfanout_bc_20260813r2`

## Статус завершения

**Серия завершена.** `supervisor_finished` в `2026-08-13T09:37:13Z`. Обе руки `probe_status=0`, `ping_status=0`, `clean_shutdown=true`. Transient unit к моменту разбора уже отсутствует. Итоговый вердикт: [результаты](latency-ws-fanout-bc-20260813r2-results.md) — `B=valid`, `C=valid`, B↔C разрешён.

Smoke и reconnect-снимки ниже — историческая запись хода серии, не финальный расчёт.

## Блок конвейера и границы

Трек `(D)`, standalone shadow: WebSocket fan-out → receive loop → raw
discard либо full in-memory decode/quote/spread. Это контролируемая серия
`B↔C` после форензики reconnect C в `wsfanout_bc_20260812r1`. Не изменяются
`app/screaner_b_o.py`, Track `(B)`, persistence, parquet, publisher, spool,
bars, compaction, backup, mount и production units.

Серия завершена; raw root на VPS сохранён. Другие работы Track D могут
читать experiment root, но нельзя его останавливать, удалять, сжимать,
архивировать или изменять. Production collector, `/data/live`, spool и mount
не трогать.

Причина прошлого C-fail и патч: [форензика](latency-ws-fanout-c-reconnect-forensics.md).

## Контролируемый профиль

Обе руки выполняются последовательно в одном durable supervisor series на NEW
VPS `root@38.180.94.108`, по `60 min` wall и `50 min` steady после `10 min`
warmup. Порядок детерминированно получают из сохранённого seed; он фиксируется
в `run_manifest.json` вместе с фактическими start/end UTC.

Неизменные knobs (как в Cdiag и BC r1): хост, universe `N=300` с XRP,
`websockets 16.1`, batch `30 pairs / 3 s`, retry `10 s`, omitted `max_queue`,
`ping_interval/timeout/close_timeout 20/20/2`, duration/warmup, raw telemetry,
resource limits, validity gates (`≤1` reconnect/exchange, `0` unrecovered,
wave `≤3/60 s`).

Единственный новый параметр **обоих** рук, не исследуемый фактор:

- `receive_mode=immediate_drain_unbounded_app_queue` — dedicated drain-task
  сразу снимает кадры с библиотечной очереди в unbounded `asyncio.Queue`,
  чтобы keepalive pong читался даже при parse/calc C.

Единственный различающий фактор B↔C прежний:

- `B`: XRP получает full decode/quote/spread, non-XRP кадры принимаются и
  дренируются с raw discard;
- `C`: XRP и все non-XRP кадры получают full in-memory
  decode/quote/spread.

Persistence отсутствует: no parquet, publisher, spool или bars.

## Предварительно объявленные gates

Рука получает `measurement_failed`, а серия не даёт причинного вывода, если
хотя бы один gate нарушен:

- до steady подтверждены `600/600` opens и subscription sends;
- не более одного unplanned reconnect на exchange, ноль unrecovered connection
  и ни одной reconnect wave более `3` событий за rolling `60 s`;
- есть полные raw XRP delivery и loop-lag artifacts, fresh ping с покрытием
  steady и без safety abort;
- сохранены runtime/reconnect forensic fields, counters waves, expected/active
  subscriptions, connection age и CPU/RSS/FD/load/memory snapshots.

Gates **не ослаблялись**.

## Ожидаемые артефакты

```text
/data/experiments/wsfanout_bc_20260813r2/
  DO_NOT_TOUCH.md
  run_manifest.json
  supervisor.status
  universe_300.json
  arm_B|C/
    arm_manifest.json
    pids.env
    runtime.jsonl
    ping_xrp.log
    xrp_delivery_okx.csv
    xrp_delivery_bybit.csv
    loop_lag.csv
```

## Запуск

Series запущена `2026-08-13T07:37:11Z` как persistent transient service
`wsfanout-bc-20260813r2.service`: supervisor PID `323810`, первая рука `B`,
probe PID `323882`, fresh matched-ping PID `323881`. Seed
`wsfanout_bc_20260813r2_order` детерминировал порядок `B → C`. Manifest
ожидает окончание серии около `2026-08-13T09:37:11Z`; это не фактический
completion verdict.

Production `spread-collector.service` остаётся `active`, PID `24505`.

## Smoke

Read-only smoke в `2026-08-13T07:40:24.440Z` (`192.635 s` B) подтверждает:

- service `active`; supervisor/probe/ping PIDs `323810` / `323882` / `323881`;
- `600/600` active connections, по `300/300` opens и subscription sends на
  OKX/Bybit; pending recovery `0`;
- `receive_mode=immediate_drain_unbounded_app_queue`;
  `recv_pending_max` OKX `2`, Bybit `10` (приложение не отбрасывает кадры;
  пик Bybit ниже типичного library `max_queue=16`);
- raw XRP delivery: `1001` OKX и `1301` Bybit строк (включая header);
  loop-lag `901` строк; ping `2329` JSONL;
- отсутствуют `unplanned_reconnect`, `connection_error`, `unplanned_close` и
  `safety_abort`;
- probe CPU `22.97%`, RSS `109.2 MiB`, FD `610`, load-1 `0.74`, available
  memory `14.00 GiB`;
- production collector по-прежнему `active`, PID `24505`.

Это warmup-only operational smoke, а не latency или causal verdict. Эти
standalone artifacts не доказывают production safety или
mounted-storage correctness.

## Reconnect snapshot — `2026-08-13T07:49:10Z` (B-only)

Read-only SSH на NEW VPS. Серия **ещё идёт**, рука **B**, elapsed
`718.639 s` (~12.0 min; warmup `600 s` уже пройден, ранний steady). Рука
**C ещё не стартовала**: каталога `arm_C/` нет. Числа C не выдумываются.

| Сигнал | Сейчас | Gate |
|---|---|---|
| service / supervisor / probe / ping | `active`; PIDs `323810` / `323882` / `323881`; `NRestarts=0` | — |
| arm | B (`receive_mode=immediate_drain_unbounded_app_queue`) | — |
| active / pending | `600/600`, pending `0` | `600/600` до steady; unrecovered `0` |
| opens / subscription sends | OKX `300/300`, Bybit `300/300` | `600/600` |
| unplanned_reconnect OKX / Bybit | `0 / 0` | ≤1 / exchange |
| unrecovered / wave | `0`; reconnect events отсутствуют, wave n/a | `0`; ≤3/60 s |
| connection_error / unplanned_close / safety_abort | `0 / 0 / 0` | — |
| keepalive `1011` / `ConnectionClosedError` | **нет** в `runtime.jsonl`, `ping_xrp.log`, journal unit | исторический C-fail |
| matched ping | `2` opens, `0` reconnect/error/keepalive | независимо от probe |
| `recv_pending_max` OKX / Bybit | `3 / 10` | не gate; очередь drain не растёт |
| production collector | `active`, PID `24505` (не тронут) | — |

Это live reconnect status, не B↔C latency и не causal verdict drain-path.
Следующая полезная точка на момент снимка была стартом C. Фактическое
завершение и расчёт: [результаты](latency-ws-fanout-bc-20260813r2-results.md).

