# EV2-09A task packet: local shadow parity and latency harness

Owner: Cursor shadow-runtime agent

Model: `cursor-grok-4.6-high-fast`

Base branch: `codex/execution-v2-dev-2026-09-18`

Reviewed predecessor: EV2-08 Gear 2.2 strategy bridge at `e386499`

Date: 2026-09-20

VPS/live authority: none

## Objective

Build the local, unwired harness needed for EV2-09 shadow qualification. It
must compare the frozen Gear 2.2 decision with the execution-v2 bridge on
equivalent slot state and exercise the execution transport hot path through a
structurally network-incapable sink.

This is EV2-09A only. It does not satisfy the 20-hour target-VPS soak or the
separate 300-eligible-signal historical replay gate. It does not install or
run a service, wire `BotRuntime`, open venue sockets, read
credentials, submit live orders, write the old `theta_trades` root, or claim
production latency. EV2-09B requires separate explicit approval.

## Pipeline block

```text
same 30-coin snapshots + quotes
          |
          +--> frozen decide_theta_k1 on equivalent SlotState
          |
          +--> Gear22StrategyBridge.observe on equivalent SpreadState
                         |
                         +--> classified decision parity
                         |
                         +--> immutable TradeIntent
                                      |
                                      v
                         prepare_dual_leg + shadow HMAC
                                      |
                                      v
                    ExecutionTransport.dispatch(NullSink x2)
                                      |
                                      +--> raw monotonic samples/histograms

ExecutionEngine.submit in mass loop ---- X
synthetic fill/FLAT lifecycle ----------- X
trade/private WebSocket ---------------- X
```

## Allowed changes

- add `app/bot/execution/shadow.py`
- add `tests/test_execution_shadow.py`
- add `docs/task-packets/EV2-09A-shadow-parity-latency.md`
- minimally amend `app/bot/execution/__init__.py` exports

Frozen: `strategy_bridge.py`, `theta_trade_manager.py`, `runtime.py`, all
`app/bot/private/**`, contracts, state machine, engine, transport, WAL,
exporters, collector, research knobs, deploy/systemd, secrets, VPS and all
existing journals/data roots.

## Locked architecture

1. Parity and latency-probe lanes are separate. They may later share one
   asyncio loop, but never share authoritative lifecycle state or fake fills.
2. Parity calls the same pure `decide_theta_k1` on an equivalent `SlotState`
   and calls only `Gear22StrategyBridge.observe` on the EV2 side. It never
   constructs `ThetaTradeManager`, sleeps 70 ms or writes `theta_trades`.
3. The mass probe never calls `ExecutionEngine.submit`. A focused test may
   characterize one real submit and prove that the state correctly stays
   `DISPATCHING` and rejects a second open.
4. `NullTradeSink` has only an owner loop and `asend(text)`. It has no URL,
   host, port, SSL, credentials, socket or private-module dependency. It
   hashes/counts bytes in memory and may yield once; it cannot connect.
5. The probe reuses `prepare_dual_leg` and `ExecutionTransport.dispatch` with
   cached metadata, deterministic plan creation and an in-memory non-venue
   HMAC/finalizer. It must exercise serialization, signing CPU and both task
   schedules without an fd or network object.
6. The shadow module must not call or contain `build_trade_intent`. Canonical
   `BridgeConfig` pins exact `GEAR22_HTML_TOP30`, `DEFAULT_OBSERVE_PARAMS`,
   `$20`, one-second TTL and `POLICY_ID`. The harness creates intents only via
   `observe()`.
7. Because shadow intents are not submitted, the parity lane explicitly calls
   `clear_inflight_on_reject(intent_id)` after handing the intent to the probe.
8. One monotonic clock domain supplies `TradeIntent.signal_mono_ns`, dispatch
   entry and sink write stamps. Wall clock is correlation only and never enters
   SLO math.
9. Never impute a missing/invalid sample as zero. Accepted probe attempts with
   missing chronometry fail the report. Rejected-before-write is counted
   separately and excluded from percentiles.
10. Store raw valid samples locally and merge counts, never percentiles.

## Public API

Keep the surface small and inert on import/construction:

```text
canonical_bridge_config(run_id) -> BridgeConfig
NullTradeSink(loop, clock)
WouldSentDecisionReplica.decide(snapshots, quotes, slot) -> ThetaDecision
ShadowParityLane.tick(...) -> ShadowParityTick
ShadowHotPath.probe(intent) -> ProbeSample
LatencyHistogram.add / merge / gate
ShadowHealth.sample(...) -> ShadowHealthSample
```

Names may follow repository conventions but capabilities must not expand. No
background thread/task starts on construction.

## Parity contract

- Empty legacy `SlotState` is compared with `initial_spread_state` IDLE/FLAT.
- Both sides use identical snapshots, quotes, `$20`, frozen params, exact
  30-coin order and book depth.
- Matching action/coin/side/reason is `match`.
- Dummy-fill occupancy, ACK-only/pending state, fill model and other expected
  differences use existing finite `DivergenceClass` values.
- Any unexplained difference fails closed; no `unknown` bucket.
- Live would-send JSONL is not read or written by EV2-09A.

## Latency sample contract

Count only post-warmup probes. Production defaults are 100 warmup and 10,000
counted attempts; tests may use smaller explicit values.

Valid requires:

- both venues `WRITE_COMPLETED`;
- `signal_to_first_write_ns`, both venue `write_latency_ns`, and
  `dual_leg_write_ns` present and non-negative;
- dual-leg value equals the slower venue write;
- transport did not report clock regression.

Counters include `valid_n`, `warmup_n`, `invalid_clock_n`, `missing_n` and
`rejected_before_write_n`. Four raw/count series are maintained:

- `signal_to_first_write_ns`
- `bybit_write_latency_ns`
- `okx_write_latency_ns`
- `dual_leg_write_ns`

Inclusive thresholds are 1, 3 and 10 ms. Nearest-rank gate math uses the total
valid count: p50 passes only when `le_1ms >= ceil(.50*N)`; p99 only when
`le_3ms >= ceil(.99*N)`; p99.9 alert is raised when
`le_10ms < ceil(.999*N)`. `invalid_clock_n` and `missing_n` must both be zero.
Local results are descriptive and cannot satisfy the target-VPS gate.

## Health schema

`bbot.execution.shadow_health.v1` always includes event-loop lag, CPU user and
system time, RSS, probe queue depth, lifecycle drops, unknown-state count,
reconnect applicability/count, WAL/durable-lag applicability and a collector
baseline object. Locally, collector observation is explicitly false and
reconnect/WAL fields are explicitly not in path; required keys are never
omitted. Sampling stays outside the transport write await.

## Tests and evidence

Required tests:

1. canonical config and exact 30-coin decision parity;
2. noncanonical order/params/notional cannot reach probe;
3. AST/source proof: no `build_trade_intent`, `app.bot.private`, URL, socket,
   Sentry, journal or runtime import/call;
4. `NullTradeSink` has no network attributes and no network call across a
   multi-probe run;
5. both sink tasks start before either is released;
6. scripted exact monotonic latency and clock-regression invalidation;
7. accepted intent with missing chronometry increments `missing_n` and fails;
8. histogram merge equals concatenated raw/count evidence;
9. nearest-rank p50/p99/p99.9 threshold vectors;
10. inflight is cleared only after the unsent intent is handed to the probe;
11. repeated probes leave the parity/probe state empty, with no fake FSM/WAL
    lifecycle;
12. one focused real-engine submit stays `DISPATCHING`, second open rejects,
    and no fills are invented;
13. health schema has every required key and local limitations are explicit;
14. import/construction starts no task, socket, file, Sentry or journal;
15. existing EV2 and legacy strategy regressions remain green.

Run locally:

```text
PYTHONPYCACHEPREFIX=/private/tmp/ev2_09a_pycache python3 -m py_compile \
  app/bot/execution/shadow.py tests/test_execution_shadow.py

PYTHONPYCACHEPREFIX=/private/tmp/ev2_09a_pycache python3 -m unittest \
  tests.test_execution_shadow tests.test_execution_strategy_bridge -v

PYTHONPYCACHEPREFIX=/private/tmp/ev2_09a_pycache python3 -m unittest discover \
  -s tests -p 'test_execution_*.py' -v

git diff --check
```

## Failure boundaries

P0: any real socket/order capability; mass `submit`; synthetic fill/FLAT;
private import; legacy journal write; wall-clock latency; zero imputation;
dropped accepted samples; percentile-of-percentiles; builder bypass; local
numbers claimed as VPS gate.

P1/deferred: NullSink and non-venue HMAC are optimistic versus a real socket;
the probe omits engine lock/accepted-event/WAL enqueue CPU; target-loop CPU,
RSS, reconnects, collector health and six-hour evidence remain unproven until
EV2-09B.

## Success criteria

- Pure local harness with a structurally impossible network path.
- Exact decision parity on equivalent state; every mismatch classified.
- Deterministic raw latency and mergeable count evidence with fail-closed
  missing/clock handling.
- Probe loop does not mutate authoritative execution state or fake fills.
- Independent critic reports no P0/P1 for EV2-09A.
- No VPS/live/canary/SLO claim.

## Implementation evidence (2026-09-20)

Implemented locally in the dedicated EV2 development worktree. No VPS,
credentials, venue sockets, external services, systemd units, live orders or
canary processes were accessed.

Changed only the allowed paths:

- `app/bot/execution/shadow.py`
- `app/bot/execution/__init__.py`
- `tests/test_execution_shadow.py`
- `docs/task-packets/EV2-09A-shadow-parity-latency.md`

The implementation keeps decision parity and transport probing as separate
lanes. `NullTradeSink` has no URL, host, port, SSL, credential or socket
surface. The repeated path uses `prepare_dual_leg` and
`ExecutionTransport.dispatch`, never `ExecutionEngine.submit`, FSM/WAL
mutation or synthetic fill/flat evidence.

Local mechanical run:

- 100 warm-up probes;
- 10,000 counted no-order probes;
- 10,000 valid samples;
- zero missing samples;
- zero invalid/mixed-clock samples;
- zero lifecycle drops;
- the supplied `SpreadState` remained `IDLE`.

These values prove the harness mechanics only. Histogram and health records
set `local_descriptive_only=true` and `target_vps_gate_eligible=false`; no
local p50/p99 is accepted as the EV2-09 target-VPS gate.

Validation evidence:

```text
shadow focused tests: 18, OK (including the 10,100-probe run)
test_execution_*.py discovery: 354, OK
tests.test_bbot_theta_trade_k1: 16, OK
tests.test_bbot_theta_live_canary: 17, OK when the two frozen Python 3.9
  runtime cases are isolated from the asyncio.run ordering issue
private warm/ACK/wire/Sentry regression: 57, OK with local loopback permission
lease-close pytest regression: 3, passed
syntax compilation: passed
git diff --check: passed
independent critic: PASS before and after focused P2 fixes; no P0/P1
```

EV2-09B was explicitly approved on 2026-09-21. It must supply the target-VPS
20-hour soak, separate 300-eligible-signal replay, target-loop 10,000-sample report,
CPU/RSS/event-loop lag, public reconnects, collector baseline and read-only
would-send comparison. This patch does not install or start that run.
