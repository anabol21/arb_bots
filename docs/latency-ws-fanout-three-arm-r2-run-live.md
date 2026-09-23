# Live record — `wsfanout_abc_20260812r2`

## Владение и границы

**Статус: `running`.** Трек `(D)`, блок: standalone shadow WebSocket
fan-out → `asyncio` loop → raw discard или in-memory parse/calc. Production
`app/screaner_b_o.py`, WebSocket ingest, parsing, spread calculation, trading,
publisher, parquet и spool не изменяются.

До фактического `end_utc` серии другим Track-D веткам разрешён только read-only
доступ. Нельзя `kill`/restart, truncate/rotate/delete/compact/reclaim experiment
root, PID или логи; также нельзя трогать production collector, `/data/live`,
`/data/spool`, production logs, mount или unit.

VPS markers:

```text
/data/experiments/wsfanout_abc_20260812r2/DO_NOT_TOUCH.md
/var/log/spread/DO_NOT_TOUCH_WSFANOUT_wsfanout_abc_20260812r2.txt
```

## Контракт повторного измерения

- Host: `root@38.180.94.108`; production остаётся active и не
  перезапускается.
- N=`300`, XRP включён; `B/C` ожидают `600` book connections и `600`
  subscription sends, `A` — `2/2`.
- Arms идут строго последовательно, `60 min` wall, `10 min` warmup исключён,
  `50 min` steady; отдельный fresh XRP ping запускается для каждой руки.
- Seed `wsfanout_abc_20260812r2` определил порядок `A → B → C`. Фактический
  порядок, start/end и PID фиксируются в `run_manifest.json`,
  `arm_*/arm_manifest.json`, `arm_*/pids.env` и `supervisor.status`.
- Каждая валидная XRP delivery sample записывается без sampling в
  `xrp_delivery_okx.csv` / `xrp_delivery_bybit.csv`. Loop lag записывается на
  каждом 200-ms callback в `loop_lag.csv`; поэтому arm-wide p50/p95/p99/max
  будут считаться из raw values, не из minute percentiles.
- Per-second `runtime.jsonl` содержит CPU%, RSS, FD, message rates, connection
  accounting и budget state. Fixed abort budgets: load-1 `<=8`, available
  memory `>=4096 MiB`, probe RSS `<=2048 MiB`, probe FDs `<=4000`, CPU
  `<=95%` одного logical CPU.
- Predeclared validity gate: любая рука с `>1` unplanned reconnect на одной
  бирже либо unrecovered connection получает `measurement_failed`. Connection
  opens, reconnects, errors, unplanned closes и subscription sends публикуются
  отдельно по exchange.

## Артефакты

```text
/data/experiments/wsfanout_abc_20260812r2/
  run_manifest.json
  supervisor.status
  arm_A|B|C/
    arm_manifest.json
    runtime.jsonl
    ping_xrp.log
    xrp_delivery_okx.csv
    xrp_delivery_bybit.csv
    loop_lag.csv
    pids.env
```

Это локальные experiment artifacts, не market-data durable target и не
доказательство mounted-storage correctness.

## Первичный smoke

Supervisor `wsfanout-abc-20260812r2.service` стартовал в
`2026-08-11T22:16:21Z`; supervisor PID `210751`. Фактический run order —
`A → B → C`, first-arm PIDs: probe `210786`, ping `210785`. Ожидаемое
окончание series — примерно `2026-08-12T01:16:21Z`; transient unit deadline —
`2026-08-12T02:16:21Z`.

Preflight перед стартом: NTP `yes`, chrony `Leap status: Normal`, system clock
`0.000018361 s` slow, `spread-collector=active` PID `24505`
(`LimitNOFILE=65535`), available memory `14.2 GiB`, load-1 `0.92`, established
TCP `1014`. Production не менялся.

Первичная попытка supervisor в `22:15:48Z` остановилась до создания `arm_A`:
shell preflight использовал недопустимое gawk variable name. Она не запускала
probe/ping и не создавала samples. Запись сохранена в `supervisor.status` и
journal; исправленный preflight прошёл и только затем начал измеряемую серию в
`22:16:21Z`.

Read-only smoke приблизительно через минуту после A start подтвердил:

- `xrp_delivery_okx.csv`: 200 flushed raw samples, preliminary raw p99
  `36 ms`, max `232 ms`;
- `xrp_delivery_bybit.csv`: 400 flushed raw samples, preliminary raw p99
  `26 ms`, max `317 ms`;
- `loop_lag.csv`: 200 flushed raw samples, preliminary raw p99 `1.906497 ms`,
  max `2.250780 ms`;
- fresh `ping_xrp.log`: 668 samples;
- runtime: `2/2` active expected connections, one subscription send on each
  exchange, CPU `2.00%`, RSS `28,971,008 bytes`, FD `12/8192`.

These are smoke values, include warmup, and are not a latency verdict. B/C
`600/600` subscription confirmation and reconnect gate are evaluated only when
their own arms complete; analysis after the complete window is separate.
