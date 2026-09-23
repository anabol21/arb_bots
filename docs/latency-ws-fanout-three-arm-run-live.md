# Live record — `wsfanout_abc_20260811r1`

## Владение и запрет вмешательства
Статус: **`finished`** (`2026-08-11T20:40:52Z`). Исторический владелец —
Track (D) Runtime Storage + Validation. Результат:
[dashboard](latency-ws-fanout-three-arm-results.md). После завершения серии
другим Track-D веткам разрешено только read-only:
**не** `kill`/restart, truncate/rotate, delete, compact или reclaim experiment
PID, logs или root. Нельзя трогать production collector, `/data/live`,
`/data/spool`, production logs, mount и production unit.

VPS markers:

```text
/data/experiments/wsfanout_abc_20260811r1/DO_NOT_TOUCH.md
/var/log/spread/DO_NOT_TOUCH_WSFANOUT_wsfanout_abc_20260811r1.txt
```

## Среда и запуск
- Host: NEW VPS `root@38.180.94.108` (`a845945761.local`).
- Supervisor unit/PID: `wsfanout-abc-20260811r1.service` / `193231`;
  `RuntimeMaxSec=4h`, `LimitNOFILE=8192`, `KillMode=control-group`.
- Experiment root: `/data/experiments/wsfanout_abc_20260811r1`.
- Runtime logs: `arm_<A|B|C>/runtime.jsonl`; matched ping:
  `arm_<A|B|C>/ping_xrp.log`; supervisor:
  `supervisor.status`.
- No parquet, spool, publisher, bars or mounted durable market-data output are
  enabled. These paths are local runtime-log artifacts only.
- Fixed manifest: `universe_300.json`, 300 pairs including XRP, generated once
  from the copied production universe; manifest file SHA-256 at arm B start:
  `bd6faf22e23b2e90a85785ab4fc047608032a6b70967244f7e3d46a0f02d3a47`.

The initial `wsfanout_abc_20260811` supervisor was stopped before its first
arm became valid: the historical ping helper imported a different `websocket`
package that lacks `WebSocketApp`, so it produced no samples. Its artifacts
are retained read-only. `r1` uses an independently smoked `websockets` ping
module and is the only running series.

## Preflight at launch
At `2026-08-11T17:33:00Z`: `NTPSynchronized=yes`; chrony Leap status was
`Normal` (RMS offset `0.000134554 s`); host load `0.86`; available memory
`14 GiB`; root/data free `62 GiB`; production `spread-collector` stayed
active as PID `24505` with `LimitNOFILE=65535`; established TCP `1016`.
The standalone service enforces `available RAM >=4 GiB`, host load-1 `<=8`,
probe RSS `<=2 GiB`, probe FDs `<=4000`; a breach writes `safety_abort` and
stops only the experiment process.

## Randomized sequential order and schedule
The fixed pre-start randomized order is `B → C → A` (seed:
`wsfanout_abc_20260811r1`). Arms run one at a time for 3600 seconds; each starts
its own fresh matched XRP ping.

| Arm | Definition | Start / expected end UTC | PID at start |
|---|---|---|---:|
| B | 300 pairs, XRP full handling; non-XRP raw drain/discard | `17:40:50Z` → `18:40:50Z` | probe `193249`, ping `193248` |
| C | 300 pairs, all streams full in-memory handling | after B → approximately `19:40:50Z` | assigned by supervisor |
| A | one XRP pair full handling baseline | after C → approximately `20:40:50Z` | assigned by supervisor |

The supervisor stops the series if an arm fails, the production unit is no
longer active, the RAM/TCP preflight gate fails, or the probe logs
`safety_abort`. Expected series completion is approximately `20:40:50Z`;
the supervisor deadline is `21:40:50Z`.

## First-arm smoke
Read-only smoke at `2026-08-11T17:41:34Z` confirmed arm B active with all
`600/600` expected book connections and `600` subscription sends. At 45 s:
OKX had `22,205` frames / `9,073,385` bytes and Bybit `21,801` frames /
`4,156,599` bytes; `299` raw control frames per exchange and respectively
`21,561` / `21,057` discarded non-XRP data frames were accounted. Non-XRP
`json.loads` was not performed; the observed `345` OKX and `445` Bybit decodes
were XRP handling only. Fresh ping already had `350` OKX and `452` Bybit
samples. Probe RSS was `109,531,136` bytes, FD count `607/8192`, threads `4`,
load-1 `0.58`, and available RAM `14.1 GiB`.
