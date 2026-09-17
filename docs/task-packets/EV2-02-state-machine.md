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
