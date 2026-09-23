# Live record — `wsfanout_cdiag_20260812r1`

## Pipeline block and boundaries

Track `(D)`, standalone shadow `C` fan-out → receive loop → full in-memory
decode/quote/spread → reconnect diagnostics. This is a diagnostic repeat, not a
production fix. `app/screaner_b_o.py`, Track `(B)`, publisher, parquet, spool,
bars, compaction, backup, retention, production unit, mounts, and production
logs are excluded.

The run is C only: `N=300`, with XRP required, `600` expected book sockets and
`600` initial subscription sends. It has a 60-minute wall window, with the
first 10 minutes excluded from latency analysis. A separate fresh XRP matched
ping runs for precisely the same window.

## Reconnect validity contract

The C arm is `measurement_failed` if any of the following holds:

- more than one unplanned reconnect on either exchange;
- any unrecovered connection at the pre-shutdown completion snapshot;
- a connection wave exceeds three reconnect events for an exchange in rolling
  60 seconds.

Every close/reconnect records exchange, exception class/string/repr, exposed
close code/reason, clean-versus-abrupt classification, attempt/socket sequence,
connection age, subscription batch, monotonic elapsed time, retry delay, and
rolling 10/60-second wave counts.

## Differential from C r2

- Subscription startup is production-shaped: 30 pairs per batch, one batch
  every 3 seconds, rather than r2's one pair every 50 ms.
- Retry delay is 10 seconds, matching the production listener, rather than 3
  seconds.
- `max_queue` is deliberately omitted, so the VPS-installed `websockets`
  default applies, as in the production source. r2 explicitly used unlimited
  `max_queue=None`; retaining that would alter receive/backpressure semantics.
  The installed `websockets` version and the omitted setting are fixed in the
  manifest.
- Full C decode/quote/spread work remains enabled for every stream. No market
  records are persisted.

## Launch record and ownership

Launched on `root@38.180.94.108` as durable service
`wsfanout-cdiag-20260812r1.service`. Supervisor PID `275998`; probe PID
`276029`; matched-ping PID `276028`. Root:
`/data/experiments/wsfanout_cdiag_20260812r1/`.

Actual start is `2026-08-12T14:36:17Z`; exact planned end is
`2026-08-12T15:36:16Z`. The service deadline is 65 minutes, so it covers the
60-minute wall run plus a bounded shutdown margin.

Before `exact_end_utc`, other Track-D work is read-only: do not kill, restart,
truncate, rotate, delete, compact, back up, retain/reclaim, or otherwise touch
the experiment root, service, PIDs, or logs. Do not modify the production
collector or its unit.

Artifacts:

```text
/data/experiments/wsfanout_cdiag_20260812r1/
  DO_NOT_TOUCH.md
  run_manifest.json
  supervisor.status
  universe_300.json
  arm_C/
    pids.env
    runtime.jsonl
    ping_xrp.log
    xrp_delivery_okx.csv
    xrp_delivery_bybit.csv
    loop_lag.csv
```

The files are standalone diagnostic artifacts. They do not establish mounted
storage correctness or production safety.

## Smoke evidence

Read-only smoke at `2026-08-12T14:39:51Z`, 214.6 seconds after the probe
started:

- service remained `active`; supervisor/probe/ping PIDs were
  `275998`/`276029`/`276028`;
- probe had `600/600` active sockets, and both exchanges had
  `300/300` opens and subscription sends (`600` total);
- raw XRP delivery files already contained `1700` OKX and `2700` Bybit samples
  (plus headers); loop-lag had `1000` samples; matched ping had `4545` events;
- first connection event included `attempt`, `socket_sequence`,
  `subscribe_batch`, connection timing and monotonic elapsed time; manifest
  includes the exact connection parameters and reconnect contract; no
  `unplanned_reconnect` event had occurred at this smoke point;
- probe CPU was `25.97%`, RSS `112.7 MiB`, FDs `610`, host load-1 `0.97`,
  and available memory `13.35 GiB`.

These are warmup-only operational observations, not a reconnect-validity or
latency verdict.
