# EV2-13B — bounded live synthetic chronometry

Status: design and notional change only, 2026-09-24. **No live orders are
authorized by this file or by the revised $10 default.** The current EV2
runtime still raises `ev2_live_execution_adapter_not_integrated`, and
`synthetic_roll_v1` still requires `LIVE_ORDERS=0` and `BBOT_BROKER=stub`.
Do not remove either gate to run this experiment on the legacy ACK-based broker.

## 1. Pipeline block

```text
synthetic signal source (one eligible coin, K=1)
  -> durable intent and two-venue readiness/ownership lease
  -> prepared $10/leg plans, WAL, warm trade WebSockets
  -> one dual-leg send (OPEN or reduce-only CLOSE)
  -> independent ACKs and private order/execution events
  -> actual filled quantities and signed REST reconciliation
  -> durable manager state; next OPEN only after both venues prove flat
```

The requested cap is six dual-leg submissions total: three OPEN and three
CLOSE, sequentially, with no new OPEN before the preceding cycle is
independently flat. Each leg has a **maximum $10 intended notional**, not a
target to round upward to an exchange minimum. An ineligible instrument is
skipped; it is not silently resized or replaced by another account/venue.
The operator explicitly permits additional **reduce-only** orders beyond
the six planned submissions solely to clear independently confirmed
residual exposure. They must use the verified remaining quantity, never
open or increase exposure, and be recorded separately from the experiment
budget. Unknown exposure is not permission for an automatic guess.

## 2. Existing files/modules

- `app/bot/runtime.py` has the deliberate live-adapter startup block.
- `app/bot/theta_trade_manager.py` has the deliberate synthetic-policy
  no-order gate and the revised future-live $10 default.
- `app/bot/execution/{engine,wal,transport,adapters}.py` contains EV2
  primitives, but not a proved production runtime connection to the sender.
- `app/bot/private/live_broker.py` and `app/bot/private/chronometry.py` have
  legacy Contour B send/chronometry. Dual ACK currently changes the local
  position before actual per-leg fills; this cannot be the EV2-13B manager.
- `app/bot/private/position_reconcile.py` and the EV2-12C task packets cover
  read-only exposure checks; quantity, pagination, ownership and freshness
  must be proven in the integrated live path.
- `app/bot/execution/live_synthetic_source.py` now parses a pure, exact
  source request (frozen 30-coin feed, one execution coin, roll seed 7,
  K=1, $10/leg, three cycles/six planned submissions). It is not wired
  into runtime and grants no order capability; both pre-existing live
  startup blocks remain active and tested.

## 3. Candidate designs

1. Enable the old `gear22_live_canary` unit with synthetic policy: forbidden
   by startup gates; its ACK-based state is not a fill/exposure proof.
2. Run a standalone W6/W7 order harness: useful transport experiment, but
   not the requested synthetic-policy-to-EV2-manager path.
3. Integrate the synthetic signal source into the reviewed EV2 fill-driven
   execution/exposure state machine, then run an isolated bounded release.
   **Selected**; no code path is declared live-ready by this packet.

## 4. Risks and failure modes

- Market orders have no application-defined maximum slippage. L1 volume
  checks and a $10 cap do not bound execution price or ensure a full fill.
- A partial/unilateral fill, lost ACK, private gap or process death must
  preserve the actual exposure and block new OPEN; no blind retry or
  fabricated FLAT. Emergency reduce-only is limited to independently
  confirmed residual quantity and must be separately audited.
- Some 30-coin instruments may not accept an order at or below $10 after
  lot/contract rounding and minimum-notional checks.
- Exchange fill timestamps use venue wall clocks; local signal/send/ACK
  intervals use monotonic time. Cross-clock `signal -> exchange fillTime`
  requires a recorded clock-offset/uncertainty bound and must not be
  presented with sub-millisecond precision unsupported by that bound.
- A fill event may arrive before or after a trade-request ACK. The report
  must not impose the requested milestones as a guaranteed temporal chain.

## 5. Minimal patch / experiment plan

1. Complete and review EV2-12B/C/D live integration; preserve both current
   fail-closed gates until the production adapter is proven end to end.
2. Add a separately armed synthetic-source-only live mode. It changes the
   signal source, not the execution, WAL, readiness or fill-state code.
3. Enforce one eligible coin, K=1, <= $10 intended notional per leg, six
   total dual-leg submission budget, three OPEN/CLOSE cycles, and pause-new-
   opens. Never count an ACK as a fill or a market close request as FLAT.
4. Record per leg: signal wall/monotonic anchor, `ws.send` start/completion
   monotonic times, request ACK receive monotonic time and result, private
   fill receive monotonic time, exchange `execTime` (Bybit) / `fillTime`
   (OKX), cumulative filled quantity, execution IDs/prices/fees, and REST
   reconciliation. Correlate with stable per-leg client IDs and WAL sequence.
   `ws.send` completion is a **local API boundary**, not a network wire tap.
5. Report `signal -> ws.send completion`, `signal -> ACK receive`, and
   `signal -> first/terminal fill` independently for each venue. Show
   exchange-time fill and local receive-time fill separately, with the
   clock-quality bound; do not substitute ACK time for fill time.

## 6. VPS/storage validation plan

Before arming, freeze a new immutable release SHA, config/policy/universe
hashes, isolated data/log roots, account fingerprint and write-capability
lease. Confirm exact account ownership, no unexpected positions or open
orders on both venues, position mode, $10 eligibility, live private and
trade socket readiness, NTP health, WAL directory fsync, independent
pause/recovery, and healthy collector/`would_sent`. Exercise deterministic
faults for partial fill, ACK-before/after-fill, one-leg send failure,
disconnect, restart and WAL uncertainty in tests and a no-order deployment.
Only then perform a fresh operator go/no-go for the live release.

## 7. Success criteria

All three requested cycles reach exchange-confirmed two-leg OPEN and
two-leg FLAT with no unexplained quantity or open order. Every submitted
order maps to one durable intent/leg ID and to ACK/private/REST evidence.
The six planned-submission budget and <= $10/leg opening cap hold; emergency
reduce-only orders are separately counted and never exceed confirmed
residual exposure. Any ambiguity stops new OPEN. Collector and `would_sent`
stay healthy. Chronometry includes the
per-venue raw milestones and honest missing/uncertain values. A partial
fill or additional recovery order requiring operator intervention is a
safety event, not a successful cycle.

## 8. Recommended next step

Implement the production EV2 live adapter and exposure reconciliation,
then run a no-order production-path preflight at $10. Do not launch an
order-capable unit or reuse the legacy ACK-based broker to satisfy this
experiment.
