# Live record — `wsfanout_bc_20260812r1`

## Блок конвейера и границы

Трек `(D)`, standalone shadow: WebSocket fan-out → receive loop → raw
discard либо full in-memory decode/quote/spread. Это контролируемая серия
`B↔C`, а не изменение production collector. Не изменяются
`app/screaner_b_o.py`, Track `(B)`, persistence, parquet, publisher, spool,
bars, compaction, backup, mount и production units.

До точного фактического завершения серии другие параллельные работы Track D
могут только читать experiment root, service, PID и логи. Нельзя их
останавливать, перезапускать, удалять, сжимать, архивировать или изменять.

## Контролируемый профиль

Обе руки выполняются последовательно в одном durable supervisor series на NEW
VPS `root@38.180.94.108`, по `60 min` wall и `50 min` steady после `10 min`
warmup. Порядок детерминированно получают из сохранённого seed; он фиксируется
в `run_manifest.json` вместе с фактическими start/end UTC.

Фиксированы: хост, universe `N=300` с XRP, `websockets` и его версия, batch
`30 pairs / 3 s`, retry `10 s`, omitted `max_queue` (default установленной
библиотеки), duration/warmup, raw telemetry, resource limits и manifest
co-resident процессов. Для каждой руки одновременно запускается отдельный
fresh XRP matched ping.

Единственный различающий фактор:

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

Фоновая среда фиксируется в manifest до серии и до/после каждой руки:
production status/PID, compactor/backup status/PID, load, clock и top process
snapshot. Эта серия не переключает background-службы.

## Ожидаемые артефакты

```text
/data/experiments/wsfanout_bc_20260812r1/
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

## Запуск и smoke

Series запущена `2026-08-12T16:45:05Z` как persistent transient service
`wsfanout-bc-20260812r1.service`: supervisor PID `282762`, первая рука `B`,
probe PID `282834`, fresh matched-ping PID `282833`. Seed
`wsfanout_bc_20260812r1_order` детерминировал порядок `B → C`. Manifest
ожидает окончание серии около `2026-08-12T18:45:04Z`; это не фактический
completion verdict.

Read-only smoke в `2026-08-12T16:47:34.887Z` (`149.606 s` B) подтверждает:

- service `active`; `600/600` active connections, по `300/300` opens и
  subscription sends на OKX/Bybit; pending recovery `0`;
- fresh raw artifacts уже содержали `900` OKX и `1300` Bybit XRP delivery
  rows (включая header), `700` loop-lag rows (включая header) и `2320`
  ping JSONL events;
- отсутствуют `unplanned_reconnect`, `connection_error`, `unplanned_close` и
  `safety_abort`;
- probe CPU `18.95%`, RSS `106.6 MiB`, FD `610`, load-1 `0.97`, available
  memory `14.06 GiB`.

Это warmup-only operational smoke, а не latency или causal verdict. Эти
standalone artifacts не доказывают production safety или
mounted-storage correctness.
