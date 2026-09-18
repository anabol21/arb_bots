# EV2-03 task packet: single-loop dual-venue transport kernel

Owner: Cursor runtime agent

Model: `cursor-grok-4.6-high-fast`

Base branch: `codex/execution-v2-dev-2026-09-18`

Reviewed predecessor: `ce66638` (EV2-02 contracts/state machine)

VPS/live authority: none

## Objective

Implement the non-owning execution-v2 transport kernel that dispatches one
prepared Bybit leg and one prepared OKX leg concurrently on the asyncio event
loop that owns both already-warm trade WebSockets. This patch proves the API,
loop ownership, timing and failure semantics only. It must not wire the kernel
into the current bot, enable a live-send path, read credentials from disk or
wait for ACK/fill traffic.

Read first:

- `AGENTS.md` in full;
- `docs/execution-v2-architecture.md`;
- `docs/execution-v2-canary-roadmap.md`, especially EV2-03;
- `docs/task-packets/EV2-02-state-machine.md`;
- `app/bot/execution/contracts.py`;
- the existing warm-socket interfaces in
  `app/bot/private/ws_warm_loop.py`, `ws_warm_session.py` and
  `ws_messages.py`;
- the existing queue/thread paths in `ws_trivial_dual_leg.py` and
  `ws_w7_parallel_dual_leg.py` as historical evidence, not as the new API.

## Pipeline block

```text
TradeIntent + exactly two LegPlan objects
                |
                v
cached immutable instrument/frame preparation (outside signal callback)
                |
                v
ExecutionTransport.dispatch() on the socket-owning asyncio loop
                |
                +---- create/schedule Bybit write ----+
                +---- create/schedule OKX write ------+  concurrently
                                                       |
                                  immutable DispatchResult / explicit error

ACK/fill/private projection is deliberately outside EV2-03.
```

## Allowed changes

- add `app/bot/execution/transport.py`;
- amend `app/bot/execution/__init__.py` only to export the EV2-03 public API;
- add `tests/test_execution_transport.py`;
- amend this task packet only to append implementation evidence.

Everything else is frozen. In particular, do not edit existing
`app/bot/private/**` behavior, `app/bot/runtime.py`,
`app/bot/theta_trade_manager.py`, `app/bot/private/live_broker.py`, collector
code, deploy/systemd files, secrets, environment files or VPS paths.

If the required same-loop semantics cannot be implemented without changing an
existing private module, stop and report the exact missing interface instead
of widening scope.

## Required public behavior

Use stdlib only for the new module. Keep value objects immutable and public
views redacted.

1. Define a narrow async socket protocol compatible with the existing
   `LoopOwnedSocket.asend(text)` boundary. The transport does not own connect,
   authenticate, subscribe, reconnect, receive or close lifecycle.
2. The kernel is bound to exactly one running asyncio loop. Both venue sockets
   must be declared as owned by that same loop; construction or dispatch with
   mixed/unknown ownership fails closed before either write.
3. Accept exactly two EV2 `LegPlan` values: one `Venue.BYBIT`, one
   `Venue.OKX`, same `intent_id`, no duplicate venue. Client ids must come from
   the EV2-02 contract and must be the venue correlation ids in the prepared
   frames.
4. Resolve and freeze instrument metadata before dispatch. The cache must
   include the OKX positive integer `instIdCode`; no metadata lookup, REST
   call, file read or environment access may occur in `dispatch()`.
5. Precompute immutable static order/frame fields outside the signal path.
   Dispatch may patch only current timestamp, deterministic request/client id
   and venue-required signature, then serialize. Inject clocks and the final
   frame builder/signer boundary so tests use no real secrets.
6. `dispatch()` must schedule both underlying `asend()` operations before
   waiting for either result. One slow/failing write must not prevent the
   other venue from being attempted. It must never call receive or wait for an
   ACK.
7. Capture monotonic nanoseconds at dispatch entry, immediately before each
   underlying `asend`, and after each completion. Return per-venue evidence
   plus `signal_to_first_write_ns` and the slower dual-leg write latency, all
   derived from the same injected monotonic clock as
   `TradeIntent.signal_mono_ns`.
8. A result must distinguish: both writes confirmed locally, one confirmed
   and one failed, both failed, cancellation after scheduling, and rejection
   before write. Never infer venue acceptance or a position from local write
   completion.
9. Cancellation must not orphan hidden send tasks. Once either send has been
   scheduled, settle/cancel and account for both children deterministically;
   surface an explicit ambiguous/cancelled outcome and never retry.
10. Do not log, journal or expose raw frames, signatures, API keys, venue
    order ids or account identifiers. Safe public evidence contains only EV2
    ids, venue, byte count, timestamps, latency and allowlisted reason codes.

The public class/function names may be chosen by the implementer, but the API
must make these invariants directly testable. Do not add an engine, risk gate,
ACK parser, retry policy, recovery order or background queue in this patch.

## Locked semantics

- Same-loop means the coroutine executing `dispatch()` is on the loop that
  owns both trade sockets. A thread hop, `run_coroutine_threadsafe`, executor,
  polling queue or blocking `.result()` is forbidden in the new hot path.
- Parallel means both send coroutines are created/scheduled before either is
  awaited to completion. It does not mean two new threads.
- Local `ws.send` completion is `write_completed`, not ACK, accepted, filled
  or open.
- No automatic retry after any possible write. Ambiguity is handed to later
  reconciliation work.
- Existing warm/private modules and the current live broker remain callable
  and unchanged, but EV2-03 does not connect them to strategy/runtime.
- Runtime performance assertions based on local fake sockets are descriptive,
  not the final 1 ms acceptance proof. Target-VPS gates belong to EV2-09.

## Tests and acceptance evidence

At minimum cover:

1. both fake sockets are owned by the active loop;
2. both writes start before either fake write is released;
3. a blocked first venue does not serialize the second venue;
4. one write raises while the peer is still attempted and recorded;
5. both writes fail without retry;
6. cancellation after scheduling leaves no pending child task;
7. dispatch from a foreign loop fails before write;
8. duplicate/missing venue and mismatched intent ids fail before write;
9. missing/stale/invalid cached metadata and invalid OKX `instIdCode` fail
   before write;
10. deterministic client/request ids and venue-safe lengths survive frame
    preparation;
11. injected-clock chronometry is exact and contains no mixed-clock or
    negative samples;
12. no receive/ACK method is invoked;
13. public result/error serialization contains no raw frame or secret;
14. importing/constructing EV2-03 does not start sockets or make live orders.

Run:

```text
PYTHONPYCACHEPREFIX=./.pycache python3 -m py_compile \
  app/bot/execution/transport.py tests/test_execution_transport.py

PYTHONPYCACHEPREFIX=./.pycache python3 -m unittest \
  tests.test_execution_transport \
  tests.test_execution_contracts \
  tests.test_execution_state_machine -v

PYTHONPYCACHEPREFIX=./.pycache python3 -m unittest \
  tests.test_warm_single_loop \
  tests.test_warm_ws_place_threadsafe \
  tests.test_dual_leg_ack \
  tests.test_sentry_integration -v

python3 -m pytest tests/test_order_lease_sol_close.py -v

git diff --check
```

Report exact test counts and the eight blocks required by `AGENTS.md`.

## Stop conditions

Stop and report instead of widening scope if:

- existing warm sockets cannot expose a same-owner-loop `asend` boundary;
- frame construction requires a new secret persistence mechanism;
- OKX `instIdCode` cannot be supplied from an immutable cache;
- the implementation would need to edit current live wiring, consume ACKs or
  change reconnect behavior;
- a test would require real exchange connectivity or live credentials.

Leave changes uncommitted for Codex review and an independent Cursor critic.
Do not push, access the VPS or start any canary.
