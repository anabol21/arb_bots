# EV2-08 task packet: Gear 2.2 strategy bridge

Owner: Cursor strategy-bridge agent

Model: `cursor-grok-4.6-high-fast`

Base branch: `codex/execution-v2-dev-2026-09-18`

Reviewed predecessor: EV2-07 recovery/restart orchestration at `839352a`

Date: 2026-09-20

VPS/live authority: none

## Objective

Build a local, unwired, deterministic bridge from the frozen Gear 2.2 K=1
decision point to immutable execution-v2 `TradeIntent` objects for the locked
30-coin universe. Preserve the existing `gear22_would_send` and legacy live
canary behavior and storage byte-for-byte.

This patch does not call `ExecutionEngine.submit`, wire `BotRuntime`, send
orders, open sockets, initialize Sentry, write journals, access the VPS, or
start a shadow/canary process. EV2-09 owns runtime/shadow wiring.

## Pipeline block

```text
30-coin snapshots + public books + proven SpreadState
                         |
                         v
              pure engine-slot projection
                         |
                         v
         existing decide_theta_k1 (frozen policy/order)
                         |
             eligible open/close only
                         v
              immutable TradeIntent
                         |
                         +--> pure parity row + finite classification

Execution ACK / ARMED / DISPATCHING --X--> OpenPosition
Execution OPEN proof + sidecar context ---> OpenPosition
Execution FLAT proof --------------------> empty slot
```

## Allowed changes

- add `app/bot/execution/strategy_bridge.py`
- add `tests/test_execution_strategy_bridge.py`
- add `docs/task-packets/EV2-08-gear22-strategy-bridge.md`
- minimally amend `app/bot/execution/__init__.py` to export the new public API

Frozen: `app/bot/theta_trade_manager.py`, `runtime.py`, all
`app/bot/private/**`, `contracts.py`, `state_machine.py`, `engine.py`, `wal.py`,
`exporters.py`, collector, research policy/parameters, deploy, secrets, VPS,
live sockets, existing journals/data roots and Sentry initialization.

## Locked decisions

1. The bridge is a sibling pure kernel in `strategy_bridge.py`.
   `ThetaTradeManager` and its default/live paths remain unmodified. The
   phrase "emits at the existing decision point" means the sibling bridge
   calls the same pure `decide_theta_k1`; it does not reuse
   `execute_decision` or `_execute_live_send`.
2. Do not widen `TradeIntent`. On open, `trade_id == intent_id`. On close,
   create a new `intent_id` and retain `trade_id == SpreadState.open_intent_id`.
3. Strategy occupancy is derived only from execution proof. ACK,
   `INTENT_ACCEPTED`, `ARMED` and `DISPATCHING` never create an
   `OpenPosition`. A sidecar `OpenTradeContext` is committed only against a
   coherent proven `OPEN`. An `OPEN` without matching context is fail-closed.
4. A local inflight latch prevents duplicate 1 Hz decisions before the engine
   changes state. It is not position authority. Submit rejection may clear it;
   state transition consumes it. EV2-08 exposes pure explicit latch methods
   but performs no submit.
5. Parity means identical `decide_theta_k1` output for identical snapshots,
   books, frozen parameters, coin order and equivalent authoritative slot.
   All other differences use a finite `DivergenceClass`; none are silently
   ignored or placed in an `unknown` bucket.
6. Intent TTL is exactly 1 second. EV2 fixtures use the live-canary notional
   `$20`, `DEFAULT_OBSERVE_PARAMS`, `POLICY_ID == gear22_frozen_v1`, and the
   exact `GEAR22_HTML_TOP30` order.

## Public pure API

The exact names may vary only for normal repository style, but the capability
must remain this small:

```text
Gear22StrategyBridge.observe(snapshots, quotes, spread_state) -> BridgeTick
project_slot(spread_state, context, inflight) -> SlotProjection
commit_open_context(spread_state, candidate) -> OpenTradeContext
Gear22StrategyBridge.commit_proven_open(spread_state, quotes) -> OpenTradeContext
restore_context(spread_state, records) -> ContextRestoreResult
build_trade_intent(decision, projection, clocks, ids, config) -> TradeIntent
```

`BridgeTick` contains the `ThetaDecision`, optional `TradeIntent`, stable
`trade_id`, parity row and exact divergence classification. No method performs
filesystem, network, database, Sentry or engine operations.

Clocks and ID creation are injected. Defaults may use stdlib clocks/UUIDs, but
tests use fixed monotonic/wall clocks and venue-safe deterministic IDs.
`signal_snapshot_ref` is a stable hash of redacted decision inputs, never a raw
book/account payload.

The inflight latch is not the open-signal memory. After an open intent is
emitted, the instance keeps a private in-memory pending-open record with side,
open theta, snapshot ref, signal mono/wall timestamps, signal timestamp and
notional. Engine ACK / ARMED / DISPATCHING may consume the inflight latch;
they must not invent or replace those signal fields. `commit_proven_open`
builds `OpenTradeContext` only against a coherent proven `OPEN`, using the
preserved signal fields plus the public-book fill spread observed at OPEN
proof. Restore stays explicit: `restore_context` on a non-OPEN state is
fail-closed `invalid_spread_state`, and a failed restore must not erase an
existing instance context.

## Authoritative status projection

| Spread status | Projected position | Pending | New open | Strategy close |
|---|---|---:|---:|---:|
| `IDLE`, `FLAT` | none | only if local inflight | yes when free | no |
| `ARMED`, `DISPATCHING` | none | yes | no | no |
| `EXPOSURE_UNKNOWN`, `RECOVERING` | none | yes | no | no |
| `CLOSING` | retained only for correlation | yes | no | no |
| `OPEN` + matching context | reconstructed from context | no | no | yes |
| `OPEN` without context | none | yes/fail-closed | no | no |
| `HALTED` | none | yes/sticky | no | no |

Direction maps `long/short` directly to `SpreadDirection.LONG/SHORT`. Close
coin and direction must match the original open state, not the unwind leg.
A local inflight CLOSE while `SpreadState` is still `OPEN` is pending, not
closeable: retain the reconstructed position only for correlation, emit no
second close decision or intent.

Canonical coin order, frozen policy params, `$20` notional and
`fill_model_diverged` are hard intent gates. Those ticks still classify the
exact divergence, but they return no `TradeIntent` and must not mark inflight.

## Open context and restart rule

`SpreadState` deliberately does not contain the Gear 2.2 open-time features
needed by close policy. Introduce immutable schema
`bbot.execution.strategy_context.v1`, keyed by
`trade_id == open_intent_id`, with at least:

- trade/open intent id, coin and side;
- open signal/fill timestamps;
- fill spread, open theta and notional;
- signal snapshot reference and monotonic/wall signal timestamps.

Commit only when `SpreadState.status is OPEN` and IDs/coin/direction match.
Fill spread comes from the public-book observation associated with the OPEN
proof, never from ACK. Clear only on coherent `IDLE` or proven `FLAT`.

EV2-08 keeps the context in memory and supports explicit restore records. On
restart, `OPEN` plus matching record restores the close-policy slot. `OPEN`
without one yields `missing_open_context`; never invent or recompute the fill
from current books. Durable projection is deferred to a later task.
`classify_divergence` raises `StrategyBridgeError('unclassified_non_match')`
when more than one divergence flag is true. Duplicate restore records for the
same `trade_id` must agree or the restore is `invalid_context`.

## Parity contract

Schema: `bbot.execution.parity.v1`. It must not be written to the old
`theta_trades` root in this patch.

Stable fields include `run_id`, `trade_id`, `intent_id`, `action`, `coin`,
`side`, mono/wall signal time, snapshot ref, policy/risk/canary versions,
coin rank, slot kind, decision action/reason, size result, notional and spread
status.

Finite divergence codes:

- `match`
- `ack_not_open`
- `pending_inflight`
- `fill_model`
- `missing_open_context`
- `engine_gate`
- `id_scheme`
- `notional_policy`
- `coin_order`
- `param_override`
- `size_gate`
- `close_while_not_open`

Any unclassified non-match is a contract error, not a new catch-all code.

## Test matrix and required evidence

New deterministic tests cover:

1. exact 30-coin order and long-first/first-eligible scanning;
2. identical decisions for legacy and projected equivalent slots;
3. open intent fields and `trade_id == intent_id`;
4. close gets a new intent while retaining the open trade id;
5. ACK-only/ARMED/DISPATCHING never commit context or create position;
6. OPEN without context fails closed; OPEN with matching context can close;
7. two observations before state acceptance emit at most one open intent;
8. every non-match fixture asserts one exact finite divergence;
9. stable snapshot hash and deterministic replay over repeated runs;
10. no journal/place/Sentry/submit call from bridge code;
11. context restore/clear and identity mismatch failures;
12. frozen params, 1-second TTL and `$20` notional.

Run locally:

```text
PYTHONPYCACHEPREFIX=./.pycache python3 -m py_compile \
  app/bot/execution/strategy_bridge.py \
  tests/test_execution_strategy_bridge.py

PYTHONPYCACHEPREFIX=./.pycache python3 -m unittest \
  tests.test_execution_strategy_bridge \
  tests.test_bbot_theta_trade_k1 \
  tests.test_bbot_theta_live_canary -v

PYTHONPYCACHEPREFIX=./.pycache python3 -m unittest discover \
  -s tests -p 'test_execution_*.py' -v

git diff --check
```

## Success criteria

- Old would-send/live-canary tests pass without modifying their implementation.
- No ACK-only state can become an EV2 strategy position or enable a close.
- Equivalent-slot replay produces the same Gear 2.2 decision deterministically.
- IDs, clocks and snapshot refs are reproducible in tests.
- Every intentional difference is explicit and classified.
- The diff is local and unwired; no VPS/live/canary claim is made.

## Implementation evidence (2026-09-20)

Implemented locally in the dedicated EV2 development worktree. No VPS,
credentials, venue sockets, external services, live orders or canary processes
were accessed.

Added / amended only the allowed paths:

- `app/bot/execution/strategy_bridge.py`
- `app/bot/execution/__init__.py`
- `tests/test_execution_strategy_bridge.py`
- `docs/task-packets/EV2-08-gear22-strategy-bridge.md`

The bridge is pure and unwired. It calls the existing `decide_theta_k1`, emits
an immutable `TradeIntent`, creates no journal/Sentry/DB/file side effects and
does not call `ExecutionEngine.submit`. The old `ThetaTradeManager`, its
would-send journal/data root and its legacy ACK-authoritative canary path were
not modified.

Safety review findings fixed before closure:

- noncanonical order/params/notional or a fill-model divergence cannot emit an
  intent or latch it;
- an inflight close blocks a second close while state remains `OPEN`;
- the original open signal fields survive until proven `OPEN`; only then can
  public proof-time books supply the fill spread;
- multiple simultaneous divergence flags fail closed instead of being
  silently collapsed;
- non-`OPEN` restore cannot report a match or erase a valid context;
- run id, deserialization and conflicting duplicate restore records fail
  closed.

Local evidence:

```text
python3 -m py_compile strategy_bridge.py + focused tests: passed
tests.test_execution_strategy_bridge: 35, OK
test_execution_*.py discovery: 336, OK
tests.test_bbot_theta_trade_k1: 16, OK
tests.test_bbot_theta_live_canary: 17, OK when the two runtime cases are
  isolated to avoid the frozen Python 3.9 asyncio.run ordering issue
private warm/ACK/wire/Sentry regression: 57, OK
lease-close pytest regression: 3, passed
git diff --check: passed
independent critic: initial FAIL on five P1 findings; post-fix PASS with no
  remaining P0/P1 for the local unwired kernel
```

The private regression requires a temporary loopback WebSocket listener; its
first sandboxed run was denied permission to bind `127.0.0.1`, and the same
test command passed 57/57 with local loopback permission. No external network
was used.

Known deferred boundary for EV2-09: runtime wiring must use `observe()` rather
than bypassing the hard gates through the lower-level intent builder; submit
rejection must call the explicit inflight-clear hook; restart restores durable
context rather than recomputing fill spread from current books.
