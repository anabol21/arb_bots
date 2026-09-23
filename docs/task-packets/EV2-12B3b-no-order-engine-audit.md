# EV2-12B3b — local no-order engine audit through WAL and readiness

Status: local API and tests only. Nothing deployed or started on the VPS;
no order capability or K=1 publication is enabled.

## Pipeline block

```text
OPEN intent -> owner / policy risk / WAL health / two private streams
  -> no-order lease (trade sockets must be absent) -> plan + metadata
  -> B3a final prewrite audit -> atomic WAL accepted/rejected audit pair
  -> FSM IDLE; no REQUEST_SENT, ACK, fill, OPEN or FLAT
```

`ExecutionEngine.audit_intent` reuses the existing plan/risk preparation
but accepts only `NoOrderTradeSocket` sentinels and OPEN. The separate
`NoOrderReadinessLease` requires both private streams ready, pause/kill clear,
and both trade-readiness flags false. Any readiness revision invalidates it;
the final transport guard checks the same lease after frame finalization.
Normal `submit` still requires real trade readiness and is unchanged.

An admitted audit appends `INTENT_ACCEPTED` and `INTENT_REJECTED` as one WAL
queue batch. The latter carries `audit_mode=no_order_prewrite` and
`prewrite_passed`; there is no `REQUEST_SENT` or fabricated exposure.
Replay returns IDLE and retains the used intent ID, preventing a duplicate
audit under that ID. A mid-drain crash could leave an ARMED prefix; restart
must treat it as blocked, not infer an order or auto-release it. The result
states `wal_accepted`, **not durable**: the runtime must drain/fsync and
verify replay before counting an attempt in a canary report.

## Safety limits and tests

The local tests cover successful repeated audits with distinct IDs,
zero socket attempts, replay without REQUEST_SENT, a failed finalizer,
duplicate ID refusal, private disconnect during finalization, absent trade
readiness for normal `submit`, WAL append failure, and refusal of CLOSE or
real trade sockets. A disconnect followed by immediate recovery still
invalidates the old audit lease.
No synthetic CLOSE is performed here because an order-free OPEN cannot
establish an exchange position to close.

This is not the final canary: the deployed policy-to-intent bridge, private
companion status ownership, durable WAL drain, manager occupancy, production
frame finalizer and service isolation are not wired to `audit_intent` yet.
The active runtime's live-start block stays. The next patch should connect
only a dedicated no-order runtime mode, then run restart/reconnect tests and
review an isolated VPS EV2-13A release. Actual ACK/fill/flatness requires a
later separately approved real-order experiment.
