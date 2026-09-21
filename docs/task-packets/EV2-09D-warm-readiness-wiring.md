# EV2-09D task packet: warm-session readiness wiring and reconnect matrix

Owner: Codex/operator

## Goal

Turn the EV2-09C abstract readiness fence into a fail-closed view of the
already-owned Bybit and OKX warm private/trade sessions. A disconnect, failed
authentication, subscription loss, sequence gap or unmatched REST reseed must
close the execution gate before a normal dual-leg write can begin.

## Pipeline

```text
warm private/trade socket callbacks
        |
        v
PrivateStreamRuntime readiness notification
        |
        v
snapshot_from_warm_session
        |
        v
WarmSessionReadinessBridge.call_soon_threadsafe
        |
        v
DualReadinessFence.publish on execution owner loop
        |
        +-- normal OPEN/CLOSE dual lease
        |
        +-- single-venue recovery lease
```

## Venue readiness

Trade readiness requires both a physically connected trade socket and a
successful authentication acknowledgement in the current reconnect generation.

Private readiness requires all of:

- a physically connected private socket;
- successful private authentication;
- subscription readiness;
- healthy sequence state;
- matched REST reseed;
- sends not blocked.

Missing attributes, malformed generations and absent runtimes fail closed.

## Reconnect policy

1. A physical socket-down callback publishes immediately; it does not wait for
   the background reconnect worker.
2. `mark_reconnect` increments the venue generation and clears private auth,
   trade auth, subscriptions, sequence health and the send gate.
3. Socket reconnect alone is insufficient. Trade auth, private auth,
   subscriptions and matched reseed must all complete before the gate reopens.
4. Publications from socket threads are sequenced and applied on the execution
   owner loop. A delayed older publication cannot overwrite a newer state.
5. Identical refreshes do not churn readiness revisions.

## Recovery boundary

Cancel and reduce-only recovery are single-venue actions. They require:

- the target venue trade socket to be connected and authenticated;
- both private streams to be fully ready;
- an unchanged connectivity revision through final frame construction.

The peer trade socket is not required for risk-reducing recovery. Pause and
kill-switch block new OPENs but do not block recovery. A last-moment readiness
change returns `not_attempted/readiness_changed` and writes zero bytes.

## Files

- `app/bot/execution/readiness.py` — immutable snapshot and ordered publisher;
- `app/bot/private/ws_private.py` — readiness notifications and explicit trade
  authentication state;
- `app/bot/private/ws_warm_session.py` — physical socket callbacks and complete
  readiness definition;
- `app/bot/execution/engine.py` — recovery readiness leases;
- `app/bot/execution/transport.py` — final single-venue pre-send guard;
- `tests/test_execution_readiness.py` — reconnect/auth/reseed publication matrix;
- `tests/test_execution_engine.py` and `tests/test_execution_recovery.py` —
  normal and recovery lease behavior;
- `tests/test_execution_transport.py` — zero-write final-guard evidence.

## Safety and exclusions

This patch opens no socket, reads no secret, changes no service, deploys
nothing and sends no order. The existing EV2-09B services remain untouched.
Controlled disconnect injection and the synthetic policy run belong to the
separate EV2-10 no-order canary.

## Success criteria

- both venues must be fully ready before normal dual-leg OPEN/CLOSE;
- an old lease never survives a disconnect/reconnect generation;
- trade auth cannot substitute for private reseed;
- private reseed cannot substitute for trade auth;
- a readiness change during finalization yields zero normal or recovery writes;
- single-venue recovery does not require the peer trade socket but does require
  both current private views;
- scoped private, warm-session, transport, engine and recovery tests pass;
- the complete execution-v2 test suite passes.
