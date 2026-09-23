# EV2-12A1 — synthetic exposure fence and legacy live-start block

Status: implemented and locally tested; this is the first EV2-12A patch, **not** a
live-ready milestone. No VPS services, credentials, configuration, or orders
are changed. The continuing frozen `would_sent` contour keeps its historical
would-fill semantics; the stricter fill model applies only to
`synthetic_roll_v1` in a new isolated data root.

## Pipeline block

```text
synthetic OPEN/CLOSE signal
  -> 70 ms simulated fill-time book
  -> size available: durable open/close lifecycle -> K=1 slot + shadow FSM
  -> size unavailable: durable *_attempt audit -> unchanged slot/shadow FSM
  -> restart: replay only committed lifecycles; validate close-attempt identity

legacy Gear 2.2 live runtime
  -> validated live flags -> explicit startup refusal before broker/Sentry
```

## Existing files/modules involved

- `app/bot/theta_trade_manager.py`: conditional synthetic lifecycle and strict
  replay. No-order `open_attempt`/`close_attempt` carries
  `lifecycle_committed=false`, `fill_outcome=unfilled_insufficient_size`,
  attempt timestamp/spread, and no fill timestamp/PnL.
- `app/bot/execution/shadow_runtime.py`: an unfilled attempt clears the
  inflight shadow intent without changing position or counting a lifecycle
  drop. A genuinely missing lifecycle row still counts as a drop.
- `app/bot/runtime.py`: fail-closed legacy live-profile startup until the
  fill-authoritative EV2 adapter is integrated.
- `tests/test_bbot_theta_trade_k1.py`, `tests/test_execution_shadow_runtime.py`,
  `tests/test_bbot_theta_live_canary.py`: regression coverage.

## Candidate designs considered

1. Keep counting synthetic closes as filled despite inadequate book size:
   preserves old counts but fabricates flatness. Rejected.
2. Change all no-order `would_sent` closes: breaks the frozen reference
   contour and historical comparison. Rejected.
3. Scope strict simulated fill to the synthetic-roll policy, record attempts
   separately, and block old live startup until the authoritative adapter is
   ready. Selected.

## Risks and remaining boundary

- Existing EV2-10/11 evidence is historical and is **not** rewritten. New
  semantics require a fresh isolated data root and run ID; do not replay the
  old EV2-11 root as if it had been produced by this patch.
- Book-size availability is only a simulation condition, not a prediction of
  actual exchange execution. Actual per-leg quantities and terminal states
  still belong to the EV2 FSM/private-event/WAL path.
- `LiveBroker.place` and the direct `ThetaTradeManager(live_send=True)` test
  harness still have legacy ACK-based local state. The deployable `BotRuntime`
  now refuses this route. EV2-12A is not complete until the exposure contract
  and durable manager publication are reviewed; EV2-12B must wire them into
  the live runtime before removing the startup refusal.
- Restart replay rejects a malformed or mismatched `close_attempt` instead
  of silently treating it as a legitimate exit.

## Minimal patch / experiment and VPS validation

Run local deterministic signal/fill book tests for unfilled OPEN and CLOSE,
same-trade-ID replay after failed CLOSE, shadow mirror rejection without
parity loss, and pre-broker live-start refusal. Then test in a *new* no-order
synthetic release/data root on the target VPS before accepting EV2-12A as
canary-qualified. Preserve collector and `would_sent`; do not arm
`LIVE_ORDERS` or start an old Gear 2.2 live unit.

Local check: 176 tests across theta manager, shadow runtime, restart
reconciliation, EV2 state machine, live-canary harness and strategy bridge;
111 further tests across execution WAL/recovery and legacy dual-leg broker.
All 287 passed under the local Python 3.9 runtime. A pre-existing logger
`ResourceWarning` appeared in a runtime test; no test failed. This is not
target-VPS or full-suite qualification.

## Success criteria and next step

- No unfilled synthetic attempt becomes a committed open or close.
- The trade journal and shadow agree after restart; a real missing row
  remains visible as a lifecycle drop.
- The deployable runtime cannot enter the ACK-authoritative live path.
- Frozen `would_sent` regression tests remain green.

Next: EV2-12A2 defines the broker/manager exposure contract against the
existing EV2 fill-authoritative state machine, with tests for ACK-only,
partial fill, reject/cancel, late fill and restart. EV2-12B then connects
that contract to durable live WAL, private events and signed reconciliation.
