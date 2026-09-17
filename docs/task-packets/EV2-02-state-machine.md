# EV2-02 task packet: execution contracts and state machine

Owner: Cursor runtime agent

Model: `cursor-grok-4.6-high-fast`

Base: reviewed `dev` after it is aligned to GitHub `main`

VPS/live authority: none

## Objective

Implement the pure, fill-authoritative domain layer for execution v2. This task
must not open sockets, read secrets, write production files, touch systemd or
change the existing live broker. It creates executable semantics that later
transport, WAL and recovery tasks will consume.

Read first:

- `AGENTS.md` in full;
- `docs/execution-v2-architecture.md`;
- `docs/b-private-journal-contract.md` sections covering order lifecycle;
- current correlation behavior in `app/bot/private/chronometry.py` and
  `app/bot/private/ws_private.py`.

## Allowed changes

- add `app/bot/execution/__init__.py`;
- add `app/bot/execution/contracts.py`;
- add `app/bot/execution/state_machine.py`;
- add focused tests under `tests/test_execution_contracts.py` and
  `tests/test_execution_state_machine.py`;
- amend this task packet only to record implementation evidence.

Everything else is frozen. In particular, do not edit `app/screaner_b_o.py`,
`app/bot/runtime.py`, `app/bot/theta_trade_manager.py`, any existing
`app/bot/private/ws_*` module, deploy files or VPS paths.

## Required interfaces

Use stdlib only. Public value objects are immutable dataclasses. Decimal values
serialize as canonical strings; timestamps are integer nanoseconds.

`contracts.py`:

- enums `IntentAction`, `SpreadDirection`, `Venue`, `LegStatus`,
  `SpreadStatus`, `ExecutionEventType`;
- immutable `TradeIntent`, `LegPlan`, `ExecutionEvent`, `LegState`,
  `SpreadState`;
- `ContractValidationError`;
- `to_public_dict()`/`from_public_dict()` round-trip methods with an explicit
  schema version and strict rejection of unknown/missing fields;
- venue-safe deterministic client-id derivation from `intent_id`, respecting
  current OKX/Bybit length/character limits without exposing venue order ids.

`state_machine.py`:

- pure `apply_event(state: SpreadState, event: ExecutionEvent) -> SpreadState`;
- `InvalidTransition` containing only redacted ids/statuses;
- `assert_invariants(state)`;
- helpers `opens_allowed(state)`, `needs_reconciliation(state)` and
  `is_proven_flat(state)`.

## Locked semantics

- ACK acceptance/rejection changes request state only; ACK cannot create or
  clear a position.
- `OPEN` requires terminal fill/position evidence for both legs and matched
  base quantity within the supplied lot tolerance.
- Partial fill, ACK timeout, stream-generation mismatch or unknown correlation
  enters `EXPOSURE_UNKNOWN`/`RECOVERING` and blocks new opens.
- A filled leg plus a confirmed-unfilled peer produces a recovery requirement;
  it never emits a new peer-open instruction.
- `FLAT` requires both venue positions and open-order sets to be reconciled
  flat; a cancel ACK alone is insufficient.
- Duplicate event ids are idempotent. Same per-intent sequence with different
  content is corruption and must fail closed.
- Events from another intent/run or events with decreasing monotonic sequence
  are rejected.
- State and public serialization must contain no API keys, signatures, raw
  frames, balances, venue order ids or account identifiers.

Do not invent transport timeouts or retry counts in this task. Model timeout,
reconnect and REST observations as input events; later tasks own scheduling.

## Tests and acceptance evidence

At minimum cover:

1. happy open and close with ACKs before fills;
2. fills before ACKs;
3. ACK-only never opens;
4. one reject plus one fill enters recovery;
5. partial fill then full fill;
6. partial fill then cancel;
7. ACK timeout/unknown correlation;
8. duplicate event replay and conflicting duplicate;
9. process-restart reconstruction from the same event list;
10. both positions flat but an open order remains;
11. stale/mismatched stream generation;
12. public serialization round-trip and forbidden-field rejection.

Run the two new test modules plus existing private tests that exercise ACK,
warm loop, lease close and Sentry. `git diff --check` must pass. Return the
eight-block report required by `AGENTS.md`, including commands and exact counts.

## Stop conditions

Stop and report instead of widening scope if the existing journal schema cannot
represent a required event, if the current client-id rules are inconsistent
between venues, or if a test would require changing existing live behavior.

## Implementation evidence (2026-09-17)

Local worktree only. No VPS access, no live credentials, no commit, no push.

Added / amended (critic-fix pass, same allowed paths only):

- `app/bot/execution/__init__.py`
- `app/bot/execution/contracts.py` (`bbot.execution.v2`)
- `app/bot/execution/state_machine.py`
- `tests/test_execution_contracts.py`
- `tests/test_execution_state_machine.py`

Existing live modules, journal v1, systemd and VPS paths were not edited.

Client ids keep the 2026-09-08 canary *shape*: OKX alphanumeric `<=32`
(`o`/`fo` + hex) and Bybit `<=36` (`b`/`fb` + the same digest truncated to
the venue cap). The hex tail is now SHA-256 of the full `intent_id` before
truncation, so OKX's dropped nibble cannot collide two UUIDs. Exact legacy
canary hex content is not required; this layer is not live.

Critic-fix semantics now locked in this domain layer: HALTED is sticky;
`INTENT_ACCEPTED` OPEN uses `opens_allowed` including `recovery_required` and
does not clear that latch; `event_id -> content_hash` is part of public
`SpreadState` serialization (same id/changed content is `corrupt_event`;
same per-intent sequence/same content under a new id is recorded);
per-intent sequence restarts at 1 after reject/new open; unmatched
`UNKNOWN_CORRELATION` does not insert a third/stub leg; FILL does not clear
UNKNOWN/RECONCILING; non-halting FAULT latches reconciliation; FLAT needs
exactly two observed-flat legs; OPEN may use fill and/or position evidence
with two positive plans; both ACK rejects return to IDLE; unhalt is out of
scope.

Journal v1 still cannot serialize fill quantities or a partial-fill type
(qty/quantity/fill_qty denylist; `terminal_update` is
filled/cancelled/expired only). This task did not extend
`bbot.private.journal.v1`. That remaining journal-v1 gap is unchanged and
is an input for later WAL/projector work (EV2-04).

Commands and counts (local Python 3.9.6):

```text
PYTHONPYCACHEPREFIX=./.pycache python3 -m py_compile \
  app/bot/execution/__init__.py \
  app/bot/execution/contracts.py \
  app/bot/execution/state_machine.py \
  tests/test_execution_contracts.py \
  tests/test_execution_state_machine.py
# pass

PYTHONPYCACHEPREFIX=./.pycache python3 -m unittest \
  tests.test_execution_contracts tests.test_execution_state_machine -v
# 42 tests, OK

PYTHONPYCACHEPREFIX=./.pycache python3 -m unittest \
  tests.test_dual_leg_ack tests.test_warm_single_loop tests.test_sentry_integration -v
# 31 tests, OK (ACK 13, warm loop 8, Sentry 10)

python3 -m pytest tests/test_order_lease_sol_close.py -v
# 3 passed

git add app/bot/execution tests/test_execution_contracts.py \
  tests/test_execution_state_machine.py docs/task-packets/EV2-02-state-machine.md
git diff --cached --check
# pass (then unstaged; files left uncommitted for independent review)
```

New execution tests: 42. Private ACK/warm/lease/Sentry regression: 34.
Combined: 76.

## Implementation evidence (2026-09-18, second safety fix)

Local worktree only. No VPS access, no live credentials, no commit, no push.
Same allowed paths only. The original 42 execution tests remain green.

Critic-reproduced blockers now locked:

- One-leg authoritative position during opening is unhedged exposure:
  `EXPOSURE_UNKNOWN`/`RECOVERING` with `recovery_required=True` until the
  second leg is proven. One fill or position plus a confirmed-unfilled peer
  enters `RECOVERING` whether the proof came from `FILL` or `POSITION`.
- `ACK_REJECTED`/`CANCELLED` plus a positive observed position cannot prove
  `OPEN`. Double reject returns `IDLE` only when neither leg has observed
  nonzero position.
- `open_evidence_is_complete` in `contracts.py` is the shared OPEN proof
  used by `SpreadState` construction/restore and `apply_event`. OPEN requires
  accepted open intent, `intent_id == open_intent_id`, exactly two positive
  non-reduce-only plans, legal `SENT`/`ACK_ACCEPTED`/`FILLED` statuses without
  ack timeout, and matched fill and/or position evidence. Forged OPEN restore
  with two SENT legs and no fill/position is rejected.
- Immutable `accepted_intent_ids` is part of the public `SpreadState` schema.
  Non-identical `INTENT_ACCEPTED` reuse after reject or a full FLAT
  round-trip is `duplicate_intent`. Exact event replay stays idempotent via
  `event_hashes`.
- Opening states: any observed nonzero position without complete two-leg
  OPEN proof requires reconciliation. CLOSING does not treat the
  pre-existing hedge as a new fault.
- Matched `RECONCILIATION` can resolve `UNKNOWN`/`RECONCILING` to terminal
  positive evidence from an authoritative matching position, so crash
  recovery can reach proven OPEN.
- CLOSE intent `coin`/`spread_direction` must match the open state;
  mismatch is fail-closed (`close_mismatch`).

Commands and counts (local Python 3.9.6):

```text
PYTHONPYCACHEPREFIX=./.pycache python3 -m py_compile \
  app/bot/execution/__init__.py \
  app/bot/execution/contracts.py \
  app/bot/execution/state_machine.py \
  tests/test_execution_contracts.py \
  tests/test_execution_state_machine.py
# pass

PYTHONPYCACHEPREFIX=./.pycache python3 -m unittest \
  tests.test_execution_contracts tests.test_execution_state_machine -v
# 54 tests, OK

PYTHONPYCACHEPREFIX=./.pycache python3 -m unittest \
  tests.test_dual_leg_ack tests.test_warm_single_loop tests.test_sentry_integration -v
# 31 tests, OK (ACK 13, warm loop 8, Sentry 10)

python3 -m pytest tests/test_order_lease_sol_close.py -v
# 3 passed

git diff --check -- app/bot/execution tests/test_execution_contracts.py \
  tests/test_execution_state_machine.py docs/task-packets/EV2-02-state-machine.md
# pass (files left uncommitted for independent review)
```

New execution tests: 54 (original 42 preserved + 12 safety-fix).
Private ACK/warm/lease/Sentry regression: 34.
Combined: 88.

## Implementation evidence (2026-09-18, third safety fix)

Local worktree only. No VPS access, no live credentials, no commit, no push.
Same allowed paths only. Prior 54 execution tests remain green; double-reject
IDLE path is replaced by the locked ACK-never-proves-FLAT rule below.

Critic NO-MERGE blockers now locked:

- Global `RECONCILIATION` (no `leg_id`) with `matched=True` may clear
  `recovery_required` and restore `stream_generation_ok` only when status is
  IDLE with no legs, FLAT with `is_proven_flat`, or EXPOSURE_UNKNOWN with
  zero legs (pre-send abort to IDLE, history/`accepted_intent_ids` retained).
  `matched=False` never clears. This recovers post-FLAT reconnect/fault
  without allowing OPEN directly.
- Two `ACK_REJECTED`/`CANCELLED` zero-fill legs stay attached in
  `RECOVERING` until both positions and open-order sets are observed zero
  and `FLATNESS_PROVEN` occurs. Then a new intent seq=1 is allowed. A late
  FILL/POSITION correlates to the retained leg and blocks opening.
- `ACK_REJECTED` after `FILLED` is contradictory evidence: retain fill qty,
  set leg UNKNOWN/RECONCILING, `ack_status=rejected`,
  `confirmed_unfilled=False`, latch recovery. Reject-then-fill and
  fill-then-reject converge.
- Observed position is authoritative even when zero. Fill plus a
  disagreeing observed quantity, including zero, cannot prove or restore
  OPEN (`observed_position_contradicts_fill` shared with restore).
- Duplicate `REQUEST_SENT` on an already-sent opening leg is
  `duplicate_send` and never resets `filled_quantity`. First reduce-only
  send on NEW close legs remains allowed. Duplicate venue/different leg
  raises `InvalidTransition` (`duplicate_venue`).
- Overfill / two-leg qty mismatch persist observed qty and enter
  UNKNOWN/EXPOSURE_UNKNOWN with `recovery_required=True`. Any blocked
  unknown exposure sets that latch.
- PARTIAL_FILL/FILL quantities are cumulative; a later smaller qty is
  `fill_regression`.
- `confirmed_unfilled` is true only when `filled_quantity==0` and no
  observed nonzero position.
- Restored `LegState.client_id` must match `derive_client_id`: non-reduce
  uses `open_intent_id`, reduce-only uses current `intent_id`.
- `PAUSE` is one-way: `pause=False` cannot unlatch.
- Reduce-only `REQUEST_SENT` while HALTED is permitted; status stays
  HALTED and non-reduce opens/sends stay rejected.

Commands and counts (local Python 3.9.6):

```text
PYTHONPYCACHEPREFIX=./.pycache python3 -m py_compile \
  app/bot/execution/__init__.py \
  app/bot/execution/contracts.py \
  app/bot/execution/state_machine.py \
  tests/test_execution_contracts.py \
  tests/test_execution_state_machine.py
# pass

PYTHONPYCACHEPREFIX=./.pycache python3 -m unittest \
  tests.test_execution_contracts tests.test_execution_state_machine -v
# 73 tests, OK

PYTHONPYCACHEPREFIX=./.pycache python3 -m unittest \
  tests.test_dual_leg_ack tests.test_warm_single_loop tests.test_sentry_integration -v
# 31 tests, OK (ACK 13, warm loop 8, Sentry 10)

python3 -m pytest tests/test_order_lease_sol_close.py -v
# 3 passed

git diff --check -- app/bot/execution tests/test_execution_contracts.py \
  tests/test_execution_state_machine.py docs/task-packets/EV2-02-state-machine.md
# pass (files left uncommitted for independent review)
```

New execution tests: 73 (prior 54 preserved + 19 third-fix).
Private ACK/warm/lease/Sentry regression: 34.
Combined: 107.

## Implementation evidence (2026-09-18, fourth safety fix)

Local worktree only. No VPS access, no live credentials, no commit, no push.
Same allowed paths only. Prior 73 execution tests remain green.

Critic NO-MERGE blockers now locked:

- Per-leg `RECONCILIATION matched=False` on FLAT sets that leg
  `RECONCILING` and atomically moves the spread to
  `EXPOSURE_UNKNOWN`/`RECOVERING` with `recovery_required=True`. Late
  nonzero position/open-order, PARTIAL/FILL, ACK_TIMEOUT, unknown
  correlation, or stream mismatch after FLAT cannot leave the spread
  open-eligible and cannot leak `ContractValidationError`. Benign repeated
  zero snapshots on already-FLAT stay FLAT.
- `apply_event` demotes illegal OPEN/FLAT intermediates before dataclass
  validation. Transition failures are `InvalidTransition`. Internal
  construction failures are caught as redacted `internal_contract`.
- Reduce-only `REQUEST_SENT` is allowed while `recovery_required` or
  status is `EXPOSURE_UNKNOWN`/`RECOVERING`/`HALTED` (and existing
  OPEN/CLOSING). Both venue legs can convert once using the current intent
  for client ids. Non-reduce sends stay forbidden. `CANCEL_REQUESTED` is
  available in recovery/HALTED when the leg exists. New opens stay blocked.
- Global `RECONCILIATION matched=True` with legs restores
  `stream_generation_ok` after a private-stream reseed but does not clear
  `recovery_required` or forge OPEN/FLAT. Empty IDLE, proven FLAT, and
  zero-leg EXPOSURE_UNKNOWN keep the pass-3 clear-latch paths. Subsequent
  per-leg position proof/reconciliation can resolve.
- Terminal FILL is cumulative. Quantity `< planned - lot_tolerance` is
  PARTIAL/UNKNOWN, latches recovery, and enters EXPOSURE_UNKNOWN. Exact
  and within-tolerance fills still prove. Smaller-than-prior qty remains
  `fill_regression` except as contradictory FLAT evidence.
- Observed nonzero open orders on an otherwise OPEN/opening non-reduce
  leg demote to recovery. Missing open-order snapshots do not block OPEN.
  A live CLOSING reduce-only working order is expected. FLAT still needs
  fresh zero-order evidence.
- `matched=True` plus stored fill cannot erase rejected/cancelled request
  evidence. UNKNOWN/RECONCILING remains unless matched reconciliation also
  has authoritative matching position proof. Fills-before-ACK still open
  without `ack_status=accepted`. Overfill upper bound remains. Contradictory
  `ack_status=rejected` is preserved (timeout may still resolve to accepted
  when position proof recovers OPEN).
- `FLATNESS_PROVEN` requires `stream_generation_ok`, both positions and
  order sets freshly observed zero, and no PARTIAL/UNKNOWN/RECONCILING or
  ack timeout. Close partial/timeout/unknown latch recovery /
  EXPOSURE_UNKNOWN instead of remaining ordinary CLOSING.
- Public restore: IDLE cannot carry legs, live fills/positions/open-order
  evidence, or active intent metadata. ARMED requires accepted open intent
  without legs. Fault-latched empty IDLE and EXPOSURE_UNKNOWN with zero
  legs remain valid. Forged OPEN relabelled IDLE is rejected.
- Accepting CLOSE and sending a reduce-only close/recovery request reset
  position/open-order observation freshness on the affected legs. Old zero
  snapshots from before the close order cannot satisfy later
  `FLATNESS_PROVEN`.
- HALTED stays sticky. Emergency reduce-only sends from HALTED remain
  HALTED. Pause remains one-way.

Limitations unchanged: journal v1 still cannot serialize fill quantities or
a partial-fill type; this task did not extend `bbot.private.journal.v1`.
No sockets, secrets, live broker, VPS, or deploy edits. Stream mismatch on
an already-proven FLAT stays FLAT with the recovery latch (not
open-eligible), matching the pass-3 reconnect test; contradictory
position/order/fill/recon evidence does demote FLAT.

Commands and counts (local Python 3.9.6):

```text
PYTHONPYCACHEPREFIX=/tmp/ev2-fix4-pyc python3 -m py_compile \
  app/bot/execution/__init__.py \
  app/bot/execution/contracts.py \
  app/bot/execution/state_machine.py \
  tests/test_execution_contracts.py \
  tests/test_execution_state_machine.py
# pass

PYTHONPYCACHEPREFIX=/tmp/ev2-fix4-pyc python3 -m unittest \
  tests.test_execution_contracts tests.test_execution_state_machine -v
# 96 tests, OK

PYTHONPYCACHEPREFIX=/tmp/ev2-fix4-pyc python3 -m unittest \
  tests.test_dual_leg_ack tests.test_warm_single_loop tests.test_sentry_integration -v
# 31 tests, OK (ACK 13, warm loop 8, Sentry 10)

PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -p no:cacheprovider \
  tests/test_order_lease_sol_close.py -q
# 3 passed

git diff --check
# pass (files left uncommitted for independent review)
```

New execution tests: 96 (prior 73 preserved + 23 fourth-fix).
Private ACK/warm/lease/Sentry regression: 34.
Combined: 130.

## Implementation evidence (2026-09-18, fifth safety fix)

Local worktree only. No VPS access, no live credentials, no commit, no push.
Same allowed paths only. Prior 96 execution tests remain green.

Critic NO-MERGE blockers now locked:

- Matched per-leg `RECONCILIATION` with a fresh authoritative
  `position_observed=True`, `position_quantity=0`,
  `open_orders_observed=True`, `open_order_count=0` snapshot may resolve
  `UNKNOWN`/`RECONCILING` timeout. A non-reduce peer with
  `filled_quantity==0` becomes `CANCELLED`/`confirmed_unfilled=True`, so
  one-leg fill plus a proven-unfilled timeout peer enters `RECOVERING`
  and reduce-only flatten remains available.
- The same fresh zero position/order proof on a reduce-only close or
  recovery leg becomes `FILLED` if cumulative `filled_quantity>0`, else
  `CANCELLED`. That terminal status can participate in
  `FLATNESS_PROVEN`. Zero remaining position is not compared to planned
  close quantity.
- `ack_status='timeout'` and `'rejected'` stay historical request
  evidence. Reconciliation no longer synthesizes `accepted`. OPEN proof
  permits timeout only on `FILLED` with matching positive position.
  FLAT proof permits timeout only on `FILLED` (filled>0) or `CANCELLED`
  (filled==0) with fresh zero position and zero open orders. Unresolved
  timeout still blocks OPEN, FLAT, and `needs_reconciliation`.
- Non-reduce reject/cancel plus a late fill still requires matching
  positive position proof before `FILLED`/`OPEN`. Fill alone plus
  `matched=True` stays `RECONCILING`. Restore cannot forge unresolved
  timeout OPEN or FLAT. `apply_event` still wraps internal
  `ContractValidationError` as redacted `InvalidTransition`.

Limitations unchanged: journal v1 still cannot serialize fill quantities or
a partial-fill type; this task did not extend `bbot.private.journal.v1`.
No sockets, secrets, live broker, VPS, or deploy edits.

Commands and counts (local Python 3.9.6):

```text
PYTHONPYCACHEPREFIX=/tmp/ev2-fix5-pyc python3 -m py_compile \
  app/bot/execution/__init__.py \
  app/bot/execution/contracts.py \
  app/bot/execution/state_machine.py \
  tests/test_execution_contracts.py \
  tests/test_execution_state_machine.py
# pass

PYTHONPYCACHEPREFIX=/tmp/ev2-fix5-pyc python3 -m unittest \
  tests.test_execution_contracts tests.test_execution_state_machine -v
# 103 tests, OK

PYTHONPYCACHEPREFIX=/tmp/ev2-fix5-pyc python3 -m unittest \
  tests.test_dual_leg_ack tests.test_warm_single_loop tests.test_sentry_integration -v
# 31 tests, OK (ACK 13, warm loop 8, Sentry 10)

PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -p no:cacheprovider \
  tests/test_order_lease_sol_close.py -q
# 3 passed

git diff --check
# pass (files left uncommitted for independent review)
```

New execution tests: 103 (prior 96 preserved + 7 fifth-fix).
Private ACK/warm/lease/Sentry regression: 34.
Combined: 137.

## Implementation evidence (2026-09-18, sixth safety fix)

Local worktree only. No VPS access, no live credentials, no commit, no push.
Same allowed paths only. Prior 103 execution tests remain green.

Critic NO-MERGE stale-evidence blocker now locked:

- Observation freshness is an event boundary, not a wall-clock TTL.
  `ACK_TIMEOUT` invalidates `position_observed` and
  `open_orders_observed` on the affected leg. Last quantities remain
  only as non-authoritative history. Filled quantity is preserved.
- `STREAM_GENERATION_MISMATCH` invalidates all current legs except an
  already-proven FLAT reconnect path (stay FLAT, recovery latch) and
  empty IDLE. `UNKNOWN_CORRELATION` invalidates the existing leg, or
  all legs when the correlation does not match. Non-idle/non-flat
  `FAULT` invalidates all current legs. HALTED remains sticky.
- Global `RECONCILIATION matched=False` invalidates all live legs.
  Per-leg `matched=False` invalidates that leg before `RECONCILING`.
  Already-proven FLAT/IDLE unmatched global recon still only latches
  recovery (pass-3 reconnect). `matched=True` never creates snapshots.
- `ACK_REJECTED` and `CANCEL_ACK` invalidate both freshness flags on
  that leg. Pre-terminal zero snapshots cannot prove FLAT. ACK alone
  still never creates or clears a position.
- Fifth-fix post-timeout observe-then-recon paths stay valid: a later
  matched reconciliation may use fresh post-uncertainty snapshots.

Limitations unchanged: journal v1 still cannot serialize fill quantities or
a partial-fill type; this task did not extend `bbot.private.journal.v1`.
No sockets, secrets, live broker, VPS, or deploy edits.

Commands and counts (local Python 3.9.6):

```text
PYTHONPYCACHEPREFIX=/tmp/ev2-fix6-pyc python3 -m py_compile \
  app/bot/execution/__init__.py \
  app/bot/execution/contracts.py \
  app/bot/execution/state_machine.py \
  tests/test_execution_contracts.py \
  tests/test_execution_state_machine.py
# pass

PYTHONPYCACHEPREFIX=/tmp/ev2-fix6-pyc python3 -m unittest \
  tests.test_execution_contracts tests.test_execution_state_machine -v
# 108 tests, OK

PYTHONPYCACHEPREFIX=/tmp/ev2-fix6-pyc python3 -m unittest \
  tests.test_dual_leg_ack tests.test_warm_single_loop tests.test_sentry_integration -v
# 31 tests, OK (ACK 13, warm loop 8, Sentry 10)

PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -p no:cacheprovider \
  tests/test_order_lease_sol_close.py -q
# 3 passed

git diff --check
# pass (files left uncommitted for independent review)
```

New execution tests: 108 (prior 103 preserved + 5 sixth-fix).
Private ACK/warm/lease/Sentry regression: 34.
Combined: 142.

## Implementation evidence (2026-09-18, seventh safety fix)

Local worktree only. No VPS access, no live credentials, no commit, no push.
Same allowed paths only. Prior 108 execution tests remain green.

Critic stale-boundary omissions now locked:

- Global `RECONCILIATION matched=False` on proven FLAT with legs
  invalidates all position/open-order freshness and atomically leaves
  FLAT for `EXPOSURE_UNKNOWN`/`RECOVERING` with `recovery_required=True`.
  A later global `matched=True` cannot restore FLAT or open-eligibility
  from pre-mismatch snapshots. Fresh both-zero observations plus
  `FLATNESS_PROVEN` are required. Empty IDLE stays latch-only.
- `STREAM_GENERATION_MISMATCH` on already-proven FLAT is unchanged:
  stay FLAT with the recovery latch; a later global matched reseed may
  restore it (pass-3 contract).
- `CANCEL_REQUESTED` invalidates observation freshness on the affected
  leg and retains quantities. Status/recovery stay otherwise unchanged.
  Pre-cancel zeros cannot prove FLAT while cancel is in flight.
  `CANCEL_ACK` still invalidates again.

Limitations unchanged: journal v1 still cannot serialize fill quantities or
a partial-fill type; this task did not extend `bbot.private.journal.v1`.
No sockets, secrets, live broker, VPS, or deploy edits.

Commands and counts (local Python 3.9.6):

```text
PYTHONPYCACHEPREFIX=/tmp/ev2-fix7-pyc python3 -m py_compile \
  app/bot/execution/__init__.py \
  app/bot/execution/contracts.py \
  app/bot/execution/state_machine.py \
  tests/test_execution_contracts.py \
  tests/test_execution_state_machine.py
# pass

PYTHONPYCACHEPREFIX=/tmp/ev2-fix7-pyc python3 -m unittest \
  tests.test_execution_contracts tests.test_execution_state_machine -v
# 110 tests, OK

PYTHONPYCACHEPREFIX=/tmp/ev2-fix7-pyc python3 -m unittest \
  tests.test_dual_leg_ack tests.test_warm_single_loop tests.test_sentry_integration -v
# 31 tests, OK (ACK 13, warm loop 8, Sentry 10)

PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -p no:cacheprovider \
  tests/test_order_lease_sol_close.py -q
# 3 passed

git diff --check
# pass (files left uncommitted for independent review)
```

New execution tests: 110 (prior 108 preserved + 2 seventh-fix).
Private ACK/warm/lease/Sentry regression: 34.
Combined: 144.

## Implementation evidence (2026-09-18, eighth safety fix)

Local worktree only. No VPS access, no live credentials, no commit, no push.
Same allowed paths only. Prior 110 execution tests remain green.

Critic global-unmatched OPEN re-promotion blocker now locked:

- Global `RECONCILIATION matched=False` with any live legs invalidates
  position/open-order freshness and marks every leg `RECONCILING` before
  recompute. `recovery_required=True`. Last-known quantities and
  working-order counts stay as non-authoritative history. HALTED stays
  sticky. Empty IDLE remains latch-only. The locked
  `STREAM_GENERATION_MISMATCH` proven-FLAT path is unchanged (stay FLAT,
  recovery latch; later global matched may restore).
- After this event, stored or repeated fills cannot re-promote `OPEN`.
  Generic global `matched=True` only restores `stream_generation_ok` and
  does not resolve `RECONCILING` legs. Fresh authoritative per-leg
  position/order evidence plus matched per-leg reconciliation is required
  to terminalize each leg; normal recovery rules then apply.
- Proven-FLAT unmatched still demotes to
  `EXPOSURE_UNKNOWN`/`RECOVERING`. Restore now needs fresh both-zero
  observations, per-leg matched recon to leave `RECONCILING`, then
  `FLATNESS_PROVEN`. The seventh-fix freshness/not-reopen assertions
  remain.

Limitations unchanged: journal v1 still cannot serialize fill quantities or
a partial-fill type; this task did not extend `bbot.private.journal.v1`.
No sockets, secrets, live broker, VPS, or deploy edits.

Commands and counts (local Python 3.9.6):

```text
PYTHONPYCACHEPREFIX=/tmp/ev2-fix8-pyc python3 -m py_compile \
  app/bot/execution/__init__.py \
  app/bot/execution/contracts.py \
  app/bot/execution/state_machine.py \
  tests/test_execution_contracts.py \
  tests/test_execution_state_machine.py
# pass

PYTHONPYCACHEPREFIX=/tmp/ev2-fix8-pyc python3 -m unittest \
  tests.test_execution_contracts tests.test_execution_state_machine -v
# 112 tests, OK

PYTHONPYCACHEPREFIX=/tmp/ev2-fix8-pyc python3 -m unittest \
  tests.test_dual_leg_ack tests.test_warm_single_loop tests.test_sentry_integration -v
# 31 tests, OK (ACK 13, warm loop 8, Sentry 10)

PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -p no:cacheprovider \
  tests/test_order_lease_sol_close.py -q
# 3 passed

git diff --check -- app/bot/execution tests/test_execution_contracts.py \
  tests/test_execution_state_machine.py docs/task-packets/EV2-02-state-machine.md
# pass (files left uncommitted for independent review)
```

New execution tests: 112 (prior 110 preserved + 2 eighth-fix).
Private ACK/warm/lease/Sentry regression: 34.
Combined: 146.

## Independent review gate (2026-09-18)

Final read-only Cursor Review Critic verdict: `PASS`; no blocking findings.
The critic re-checked the prior fail-open classes, including stale evidence
across timeout/reject/cancel/fault/reconciliation boundaries, global unmatched
reconciliation on fill-proven OPEN and proven FLAT, recovery reduce-only sends,
under/overfill, working orders, strict restore, idempotency, HALTED and pause.

Root verification after the accepted review diff:

```text
python3 -m py_compile ...
# pass

python3 -m unittest \
  tests.test_execution_contracts tests.test_execution_state_machine -q
# 112 tests, OK

python3 -m unittest \
  tests.test_dual_leg_ack tests.test_warm_single_loop tests.test_sentry_integration -q
# 31 tests, OK

python3 -m pytest -p no:cacheprovider tests/test_order_lease_sol_close.py -q
# 3 passed

git diff --check
# pass
```

Review-gate total: 146 tests. The worktree remains uncommitted. No merge,
push, VPS/live access, canary, Grok-bot notification, or runtime wiring was
performed by EV2-02.
