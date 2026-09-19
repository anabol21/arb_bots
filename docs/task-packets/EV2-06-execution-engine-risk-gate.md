# EV2-06 task packet: ExecutionEngine and risk gate

Owner: Cursor runtime agent

Model: `cursor-grok-4.6-high-fast`

Base branch: `codex/execution-v2-dev-2026-09-18`

Reviewed predecessor: EV2-05 WAL/replay/projector/exporters, critic `PASS`

Date: 2026-09-19

VPS/live authority: none

## Objective

Compose the frozen EV2 contracts, fill-authoritative state machine, same-loop
transport, private `AdapterBatch` projection and WAL admission kernel behind
one `ExecutionEngine`. The engine owns exactly one shared-account
`SpreadState`, serializes `submit` plus adapter ingestion with one
`asyncio.Lock`, applies `INTENT_ACCEPTED` before dispatch so simultaneous
OPENs across coins enforce `K_live=1` from the FSM, and never treats ACK
counters as slot authority.

This patch does not wire the current runtime, send live orders, call venue
REST or WebSockets, drain/fsync the WAL, initialize Sentry, edit systemd,
access the VPS or start a canary. Legacy `would_sent` remains operationally
read-only. Existing private modules remain behaviorally unchanged.

Read first:

- `AGENTS.md` in full;
- `docs/execution-v2-architecture.md`;
- `docs/execution-v2-canary-roadmap.md`, especially EV2-06;
- EV2-02 through EV2-05 task packets;
- `app/bot/execution/contracts.py`, `state_machine.py`, `transport.py`,
  `adapters.py`, `wal.py`.

## Pipeline block

```text
TradeIntent
      |
      v
cached risk / readiness / ownership / WAL-capacity gates
      |
      v
INTENT_ACCEPTED applied under asyncio.Lock   <-- K_live=1
      |
      +--> WAL enqueue (admission only, no drain/fsync)
      |
      v
same-loop ExecutionTransport.dispatch()
      |
      +--> REQUEST_SENT / ACK_TIMEOUT / FAULT / INTENT_REJECTED
      |
AdapterBatch --(same lock)--> pre-fold + capacity + enqueue + commit
```

## Allowed changes

- add `docs/task-packets/EV2-06-execution-engine-risk-gate.md`;
- add `app/bot/execution/engine.py`;
- add `app/bot/execution/ownership.py`;
- add `tests/test_execution_engine.py`;
- amend `app/bot/execution/__init__.py` only to export the reviewed EV2-06 API;
- amend `app/bot/execution/wal.py` only for pure admission-capacity checks and
  an all-or-nothing in-memory batch enqueue API.

Everything else is frozen, including `contracts.py`, `state_machine.py`,
`adapters.py`, `transport.py` behavior, `exporters.py`, all existing
`app/bot/private/**`, runtime/strategy/collector code, deploy/systemd, secrets
and VPS paths.

If a safe mapping needs a new EV2 event type or an illegal frozen transition,
stop and report the exact gap. Do not invent ACK/fill events.

## Required public design

Use stdlib only. Public value objects are immutable and redacted.

```text
SCHEMA_VERSION = "bbot.execution.engine.v1"

RiskPolicy
ReadinessSnapshot
SubmitStatus {accepted, rejected, recovery_required}
SubmitResult
IngestResult
ExecutionEngine
FileOwnershipFence
OwnershipError
admission_capacity_ok(...)
ExecutionWal.can_admit(count, *, open_intent)
ExecutionWal.enqueue_batch(events)
```

`submit(intent)` uses the injected synchronous `plan_resolver(intent) ->`
exactly two `LegPlan` values, existing `InstrumentCache` / `prepare_dual_leg`,
`ExecutionTransport`, `ExecutionWal`, and a monotonic clock. The only await
on the hot path is the parallel socket writes inside `dispatch()`.

## Locked semantics

- One shared account `SpreadState`. `opens_allowed(state)` is the live slot
  gate. ACK counters never allocate or free the slot.
- Apply `INTENT_ACCEPTED` before dispatch while still holding the lock.
- OPEN gates: TTL, coin allowlist, `0 < notional <= 20 USDT` cap,
  `opens_allowed`, WAL health/capacity, both trade sockets, both private
  streams/generations, metadata freshness, active ownership, pause and kill
  switch.
- Pause, kill switch and `health.blocks_opens` block OPEN only. CLOSE and
  recovery remain allowed when reserved-tail capacity exists.
- Pre-accept risk rejects enqueue no WAL event.
- Every accepted decision and every derived lifecycle event is WAL-enqueued.
- Before any accepted write, prove WAL room for the worst-case batch:
  accepted + two `REQUEST_SENT` + two uncertainty/fault events (5).
- An OPEN worst-case batch must fit before the reserved tail, leaving the full
  tail available for CLOSE/recovery lifecycle evidence.
- Engine is the sole enqueue owner and holds the lock across
  dispatch/ingestion, so there is no admission race. No drain/fsync.
- Precompute `apply_event`, atomically enqueue the full in-memory batch, then
  commit. Batch validation/encoding failure does not advance the WAL cursor or
  hash chain.
- OPEN plans must be non-reduce-only and CLOSE plans must be reduce-only. Both
  request shapes are pre-folded through the FSM before any socket write.
- `AdapterBatch` ingestion pre-folds the entire batch and proves capacity
  before enqueueing any event.
- Event order: `INTENT_ACCEPTED`; then BYBIT then OKX. Emit `REQUEST_SENT`
  for `WRITE_COMPLETED` or any evidence with `asend_start_mono_ns`. Failed or
  cancelled started writes then emit `ACK_TIMEOUT`. If a write may have
  started but cannot be represented per-leg, emit a valid halt/recovery
  `FAULT`. `REJECTED` with no starts may roll back OPEN via
  `INTENT_REJECTED`. CLOSE or any ambiguous/started write returns
  `recovery_required` and latches via existing valid events.
- `FileOwnershipFence` uses stdlib `fcntl.flock`, explicit nonblocking
  acquire on an injected path, a stable owner token, in-memory
  owned/assert on submit, and explicit release. No filesystem call per
  submit. The same path cannot be owned twice, one acquire can be claimed by
  only one engine, and a forked child cannot use the inherited claim.
- Readiness is replaceable under the engine lock. Submit results retain
  `run_id` and the redacted transport chronometry/evidence when dispatch ran.
- Task cancellation is propagated after recovery evidence is committed.

## Tests and acceptance evidence

At minimum cover:

1. simultaneous and stress OPENs across coins => exactly one dispatch;
2. all OPEN risk gates;
3. CLOSE allowed under pause/kill/`blocks_opens` when tail capacity exists;
4. transport rejected / partial / failure / cancel mapping;
5. `AdapterBatch` atomicity;
6. WAL capacity no-send;
7. ownership same-process and subprocess conflict/release;
8. no fsync/drain/export/log on submit.

Run:

```text
PYTHONPYCACHEPREFIX=./.pycache python3 -m py_compile \
  app/bot/execution/engine.py app/bot/execution/ownership.py \
  app/bot/execution/wal.py tests/test_execution_engine.py

PYTHONPYCACHEPREFIX=./.pycache python3 -m unittest \
  tests.test_execution_engine \
  tests.test_execution_wal \
  tests.test_execution_exporters \
  tests.test_execution_adapters \
  tests.test_execution_transport \
  tests.test_execution_contracts \
  tests.test_execution_state_machine -v

PYTHONPYCACHEPREFIX=./.pycache python3 -m unittest \
  tests.test_warm_single_loop \
  tests.test_warm_ws_place_threadsafe \
  tests.test_dual_leg_ack \
  tests.test_wire_transcript \
  tests.test_sentry_integration -v

python3 -m pytest tests/test_order_lease_sol_close.py -v

git diff --check
```

Report exact counts and the eight blocks required by `AGENTS.md`.

## Stop conditions

Stop and report instead of widening scope if:

- a required latch needs an event type or transition the frozen FSM rejects;
- fsync, Sentry, metrics, PostgreSQL or logging must execute in submit;
- ownership fencing requires editing legacy private/would_sent code;
- tests would need VPS, credentials or live venue access.

Leave implementation changes uncommitted. Do not push, access the VPS or
start any canary.

## Implementation evidence (2026-09-19)

Local worktree only. No VPS access, no live credentials, no commit, no push.
No network services, PostgreSQL, Sentry SDK, sockets or live exchanges.

Added / amended (allowed paths only):

- `docs/task-packets/EV2-06-execution-engine-risk-gate.md`
- `app/bot/execution/engine.py`
- `app/bot/execution/ownership.py`
- `tests/test_execution_engine.py`
- `app/bot/execution/__init__.py` (EV2-06 public API export only)
- `app/bot/execution/wal.py` (`admission_capacity_ok`, atomic
  `ExecutionWal.enqueue_batch`, `ExecutionWal.can_admit`, and
  `SUBMIT_WORST_CASE_EVENTS=5`)

Frozen modules were not edited: `contracts.py`, `state_machine.py`,
`adapters.py`, `transport.py`, `exporters.py`, `journal_v1.py`,
`sentry_setup.py`, all `app/bot/private/**` behavior, runtime/strategy/
collector, deploy/systemd, secrets and VPS paths. Stop conditions were not
reached: every latch uses an existing EV2 event/transition; submit never
drains, fsyncs, logs, exports or talks to Sentry/PostgreSQL; ownership is a
new stdlib fence and does not change `would_sent`.

Locked in this kernel:

- One shared-account `SpreadState` and one `asyncio.Lock` across `submit`
  and `AdapterBatch` ingestion.
- `INTENT_ACCEPTED` is applied and WAL-enqueued before dispatch so
  simultaneous OPENs across coins take the FSM `K_live=1` slot.
- OPEN gates: TTL, allowlist, `0 < notional <= 20`, `opens_allowed`, WAL
  health/capacity, both trade sockets, both private streams/generations,
  metadata, ownership, pause and kill switch.
- Pause, kill switch and `health.blocks_opens` never block CLOSE when
  reserved-tail capacity exists.
- Pre-accept rejects enqueue no WAL event. Accepted decisions and derived
  lifecycle events are atomically enqueued. Worst-case admission is 5 events
  and cannot consume the reserved CLOSE/recovery tail.
- Plan `reduce_only` semantics and both FSM request shapes are proven before
  dispatch. Readiness updates share the engine lock, cancellation propagates
  after recovery evidence, and one ownership acquire has one engine claim.
- Event order: `INTENT_ACCEPTED`; BYBIT then OKX. `REQUEST_SENT` for
  `WRITE_COMPLETED` or `asend_start_mono_ns`; started failures/cancels then
  `ACK_TIMEOUT`; unrepresentable possible starts emit halt `FAULT`. OPEN
  `REJECTED` with no starts rolls back via `INTENT_REJECTED`. CLOSE or
  ambiguous/started writes return `recovery_required`.
- `FileOwnershipFence` uses nonblocking `fcntl.flock`, a stable owner
  token, in-memory `assert_owned` on submit, and explicit release.
- After `plan_resolver` returns and immediately before `INTENT_ACCEPTED`
  is WAL-committed, monotonic time is read again; `now >= expiry` rejects
  `ttl_expired` with no send and no WAL growth. The accepted event uses
  that final valid monotonic.
- `acquire()` creates a new claim generation every time.
  `assert_owned(claim_token)` requires `_claimed` and an exact current
  token. The engine re-asserts immediately before accepted WAL commit and
  again immediately before dispatch. Lost ownership after OPEN accept
  rolls back via `INTENT_REJECTED`; CLOSE latches recovery `FAULT`;
  neither path sends.

Commands and counts (local Python 3.9.6), final full validation:

```text
PYTHONPYCACHEPREFIX=./.pycache python3 -m py_compile \
  app/bot/execution/engine.py app/bot/execution/ownership.py \
  app/bot/execution/wal.py tests/test_execution_engine.py
# pass

PYTHONPYCACHEPREFIX=./.pycache python3 -m unittest \
  tests.test_execution_engine \
  tests.test_execution_wal \
  tests.test_execution_exporters \
  tests.test_execution_adapters \
  tests.test_execution_transport \
  tests.test_execution_contracts \
  tests.test_execution_state_machine -v
# 259 tests, OK (engine 43, wal 36, exporters 13, adapters 28,
# transport 27, contracts+state machine 112)

PYTHONPYCACHEPREFIX=./.pycache python3 -m unittest \
  tests.test_warm_single_loop \
  tests.test_warm_ws_place_threadsafe \
  tests.test_dual_leg_ack \
  tests.test_wire_transcript \
  tests.test_sentry_integration -v
# 57 tests, OK outside the sandbox because three tests bind temporary
# loopback sockets. The known reconnect timing test failed once, then passed
# both its isolated rerun and the full-block rerun. No frozen file was edited.

python3 -m pytest tests/test_order_lease_sol_close.py -v
# 3 passed

git diff --check
# pass (files left uncommitted)
```

New EV2-06 tests: 43.
EV2-05 WAL/exporters: 49 preserved.
EV2-04 adapter tests: 28 preserved.
EV2-03 transport tests: 27 preserved.
EV2-02 contracts/state machine: 112 preserved.
Private warm/ACK/wire/Sentry regression: 57.
Lease close pytest: 3.
Combined: 319.

Independent review history:

- initial critic verdict: `FAIL` because plan `reduce_only` was checked only
  after socket writes, readiness was construction-only, WAL batches were not
  atomic, cancellation was swallowed, and forked ownership was not fenced;
- second critic verdict: `FAIL` on post-resolver TTL and stale claim
  after fence reacquire;
- both remaining blockers now have focused regression coverage in
  `tests/test_execution_engine.py`;
- final post-fix critic verdict: `PASS` for commit as an unwired local kernel,
  explicitly not for deploy/live/canary.

Residual P2 risks carried to EV2-07 review:

- the post-accept/pre-dispatch ownership-loss rollback path reuses already
  tested `INTENT_REJECTED` / `FAULT` transitions but has no dedicated injected
  timing hook;
- if that rollback's WAL batch unexpectedly fails despite the reserved tail,
  the engine remains fail-closed for sends but should surface
  `recovery_required` rather than a plain rejection.
