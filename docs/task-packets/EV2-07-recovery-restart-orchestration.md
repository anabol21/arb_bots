# EV2-07 task packet: recovery and restart orchestration

Owner: Cursor runtime agent

Model: `cursor-grok-4.6-high-fast`

Base branch: `codex/execution-v2-dev-2026-09-18`

Reviewed predecessor: EV2-06 ExecutionEngine / risk gate at `46a522c`

Date: 2026-09-20

VPS/live authority: none

## Objective

Make every non-happy EV2 lifecycle reachable and bounded on the local
unwired kernel. Recovery plans and dispatches **one venue action at a
time**. `ExecutionEngine.submit` and `ExecutionTransport.dispatch` stay
strictly dual-leg. Recovery is not a `TradeIntent` action.

This patch does not wire the current runtime, send live orders, call
venue REST or WebSockets, drain/fsync the WAL on `submit`, initialize
Sentry, edit systemd, access the VPS or start a canary.

## Pipeline block

```text
SpreadState + Readiness + attempts
      |
      v
pure plan_recovery()
      |
      +-- WAIT_RESEED / NOTHING / HALT / PROVE_FLAT
      |
      +-- CANCEL_PEER | FLATTEN_FILLED
              |
              v
same asyncio.Lock as submit / ingest
      |
      +--> WAL admit with reserved recovery tail (open_intent=False)
      |
      v
ExecutionTransport.dispatch_action()   <-- one venue, no sibling
      |
      +--> CANCEL_REQUESTED | REQUEST_SENT | ACK_TIMEOUT | FAULT
      |
AdapterBatch --(same lock)--> evidence only, then re-plan
```

## Allowed changes

- add `app/bot/execution/recovery.py`
- add `tests/test_execution_recovery.py`
- add `docs/task-packets/EV2-07-recovery-restart-orchestration.md`
- amend `app/bot/execution/transport.py` with a one-venue action path
- amend `app/bot/execution/engine.py` with same-lock recovery/restart
- amend `app/bot/execution/adapters.py` for flatten client identity
- minimally amend `app/bot/execution/wal.py` to advance the restart
  reconciliation epoch and return its internally validated venue without
  changing the token format
- amend `app/bot/execution/__init__.py` to export only the new public API
- amend focused WAL/adapter tests for the demonstrated critic blockers

Frozen: `contracts.py`, `state_machine.py`, `wal.py` token format,
`exporters.py`, all `app/bot/private/**`, collector, strategy, deploy,
secrets, VPS, live sockets.

## Locked decisions

- Finite public actions: `WAIT_RESEED`, `CANCEL_PEER`, `FLATTEN_FILLED`,
  `PROVE_FLAT`, `HALT`, `NOTHING`. No arbitrary instrument, side,
  quantity, cancel-all, venue order id, resume, or unhalt API.
- Flatten venue and quantity come from current `SpreadState` plus an
  injected constrained factory. Never chase a missing leg or resend a
  non-reduce open.
- `submit` / `dispatch` remain dual-leg. Recovery uses
  `dispatch_action` only: cancel by known client id, or reduce-only
  place. No ACK wait, no retry, no sibling `NOT_ATTEMPTED`.
- Engine is the sole WAL enqueue owner. Re-plan under the lock
  immediately before action. Opens stay blocked. Recovery may act when
  pause or kill switch block opens, but requires the target trade
  socket. Persist only legal existing FSM events.
- WAL admission uses reserved recovery capacity
  (`RECOVERY_WORST_CASE_EVENTS = 4`, `open_intent=False`).
- Never send if lifecycle admission, folding or ownership fails.
  Ambiguous started writes require reconciliation; no blind retry.
- Sticky `HALTED`: never clear it and never treat `FLATNESS_PROVEN` as
  legal from `HALTED`.
- Adapter: keep primary `register` dual-leg; `bind_recovery_plan` adds a
  reduce-only client id without removing open bindings. Fill-piece,
  last-fill, cancel and reject dedupe key by client id / order identity.
- Restart: WAL replay plus live evidence. Old-process tokens are
  invalid. Only new durable matched per-venue tokens may clear the
  restart gate. Empty IDLE may emit one deterministic
  `restart:{run_id}` correlation on both engine-authored per-venue
  reconciliation events after an injected complete live snapshot proves
  the watched set flat.
- `LegState` is not widened. Instrument and side come from the factory.

## Failure matrix

| Fault | Expected |
|---|---|
| Peer reject + one fill | Flatten filled venue only |
| Unresolved timeout | `WAIT_RESEED`; no cancel/flatten |
| Resolved timeout + zeros | `RECOVERING` then flatten filled only |
| Peer still working | `CANCEL_PEER` first |
| Late peer fill before flatten | Re-plan; flatten aborted |
| Partial, no position | `WAIT_RESEED` |
| Partial + position + unfilled peer | Flatten observed qty |
| Overfill / qty conflict | No flatten; eventually `HALT` |
| Flatten fill == open fill | Ingested after `bind_recovery_plan` |
| Second cancel on flatten client | Not deduped away |
| Flatten without bind | `unknown_correlation` |
| Stale stream | Ordinary ingest blocked; REST required |
| Incomplete REST | No matched token; no `FLATNESS_PROVEN` |
| Crash after durable flatten `REQUEST_SENT` | No second flatten |
| Crash before flatten write | Fill intact; may flatten once after restart rebind |
| Crash after enqueue, before drain | In-memory admit forgotten. Enqueue is not durable: restart still requires REST reseed plus the deterministic reduce-only client id. Do not fsync on submit or recovery send. |
| Restart empty IDLE | `restart:{run_id}` tokens |
| Restart FLAT / stale token | New tokens required; stale rejected |
| Restart RECOVERING | Single flatten after rebind; peer not written |
| Restart HALTED | Stays `HALTED`; prove-flat rejected |
| Flatten no `asend` start after admit | Keep `REQUEST_SENT`; no rollback; no second flatten; `WAIT_RESEED` |
| Flatten started then failed | Admitted `REQUEST_SENT` plus `ACK_TIMEOUT`; no resend |
| Prove flat without fresh zeros | Rejected |
| Double reject both zero | No flatten; `FLATNESS_PROVEN` |
| `submit(CLOSE)` while recovering | `close_not_open` |
| `submit(OPEN)` while recovering/paused | Rejected; flatten still allowed |
| Dual-leg `dispatch` for recovery | Orchestrator never calls it |
| Concurrent submit + recovery | Same lock; `K_live=1` |
| WAL reserved tail | 6-event dual batch fails; 3-event reseed fits |
| Ownership lost | No write |
| Late fill after flatten | Cannot return `OPEN` |
| Successful recovery | Both venues positions and open-order sets freshly zero |

## Tests and evidence

Run:

```text
PYTHONPYCACHEPREFIX=./.pycache python3 -m py_compile \
  app/bot/execution/recovery.py app/bot/execution/engine.py \
  app/bot/execution/transport.py app/bot/execution/adapters.py \
  tests/test_execution_recovery.py

PYTHONPYCACHEPREFIX=./.pycache python3 -m unittest \
  tests.test_execution_recovery \
  tests.test_execution_engine \
  tests.test_execution_wal \
  tests.test_execution_exporters \
  tests.test_execution_adapters \
  tests.test_execution_transport \
  tests.test_execution_contracts \
  tests.test_execution_state_machine -v

git diff --check
```

## Implementation evidence (2026-09-20)

Local worktree only. No VPS access and no live credentials.
No network services, PostgreSQL, Sentry SDK, sockets or live exchanges.

Added / amended (allowed paths only):

- `app/bot/execution/recovery.py`
- `app/bot/execution/transport.py` (`unsigned_cancel_finalizer`,
  `prepare_venue_action`, `dispatch_action`; `dispatch()` unchanged)
- `app/bot/execution/engine.py` (flatten admit-before-dispatch, restart
  latch, ownership re-assert, trusted primary identity)
- `app/bot/execution/adapters.py` (`primary_plan`, `reset_for_restart`)
- `app/bot/execution/wal.py` (`begin_restart` epoch advance;
  `mark_venue_reconciled` returns the internally bound venue; token
  format unchanged)
- `tests/test_execution_recovery.py`
- `tests/test_execution_wal.py`
- `tests/test_execution_adapters.py`
- `docs/task-packets/EV2-07-recovery-restart-orchestration.md`

Frozen modules were not edited: `contracts.py`, `state_machine.py`,
WAL token format, `exporters.py`, `ownership.py`, all
`app/bot/private/**`, runtime/strategy/collector, deploy/systemd,
secrets and VPS paths.

The Cursor implementation sessions could not execute `python3` /
`unittest`; Codex ran the required local gates independently:

```text
PYTHONPYCACHEPREFIX=./.pycache python3 -m py_compile \
  app/bot/execution/recovery.py app/bot/execution/engine.py \
  app/bot/execution/transport.py app/bot/execution/adapters.py \
  tests/test_execution_recovery.py

PYTHONPYCACHEPREFIX=./.pycache python3 -m unittest \
  tests.test_execution_recovery \
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
  tests.test_sentry_integration -q

python3 -m pytest tests/test_order_lease_sol_close.py -p no:cacheprovider -q

git diff --check
```

Evidence (local worktree only):

- execution-v2 tests: 301, OK (including the EV2-07 recovery fault
  matrix and all preserved EV2-02..06 suites)
- frozen private warm/ACK/wire/Sentry regression: 57, OK; the known
  reconnect timing test failed once before the critic fixes, then passed
  both its isolated rerun and the full-block rerun; the final post-fix
  57-test run passed directly
- lease-close regression: 3, passed
- syntax compilation: passed
- `git diff --check`: passed
- independent critic: initial `FAIL` on send-before-admit, restart latch,
  ownership recheck and trusted instrument/side identity; post-fix `PASS`
  with no remaining P0/P1 for the unwired local kernel
- VPS / live / canary: not run, not claimed

## Critic-fix notes (2026-09-20)

In-process flatten admit happens before `dispatch_action`. That enqueue is
admission, not durability: it does not fsync or drain. A process crash
before drain still depends on mandatory restart REST reseed and the
deterministic reduce-only client id. Do not claim the admit is durable.

`begin_restart` advances the WAL reconciliation epoch and sets an engine
restart latch. `_open_gate` honors that latch even when the same in-memory
WAL was previously `mark_venue_reconciled`. The latch clears only after two
new current-epoch matched durable venue tokens.

Trusted instrument and side come from the registered primary non-reduce
binding (adapter `primary_plan` or the engine's last OPEN plans).
`LegState` is not treated as if it stored those fields.

## Explicit no-live / no-VPS boundary

No credentials, no venue sockets, no collector, no `app/bot/private/**`,
no systemd, no mount, no PostgreSQL, no Sentry SDK. Local unittest
proves only the unwired kernel. This packet does not claim canary
readiness.

## Stop conditions

Stop and report instead of widening scope if:

- a required latch needs an event type or transition the frozen FSM
  rejects;
- token format must change;
- tests would need VPS, credentials or live venue access.

Commit and push only after the local gates and independent critic pass.
Do not access the VPS or start any canary in EV2-07.
