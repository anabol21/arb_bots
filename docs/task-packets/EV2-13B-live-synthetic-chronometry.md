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
The operator selected **CAP** and explicitly allows a matched size below
$10/leg. A fresh two-venue preflight is still required for every attempt.
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
- `ExecutionEngine(durable_prewrite=True)` is an **opt-in, local** bridge
  primitive: after enqueueing `INTENT_ACCEPTED`, it drains/fsyncs the WAL,
  replays it, and requires the durable accepted record and full replayed
  state to match before transport dispatch. Any failed proof returns
  `recovery_required` and leaves the armed intent for reconciliation, with
  no socket send. It also rechecks TTL after this work. The default remains
  off for legacy/no-order hot-path compatibility; no live runtime enables
  it yet. A live adapter must explicitly enable and test it.
- `app/bot/execution/live_cap_sizing.py` is a pure prospective sizing gate
  for CAP. Given *fresh* public metadata and executable L1 sides, it floors
  OKX contracts so both intended notionals remain <=$10 and the Bybit base
  quantity exactly matches OKX `contracts × ctVal`; it never rounds a leg up
  to satisfy a minimum. It is not wired to the sender and does not bound
  actual market-order fill price.

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
- CAP is particularly sensitive to size steps. On the target VPS public
  instrument snapshot of 2026-09-24, OKX `CAP-USDT-SWAP` had `ctVal=100 CAP`,
  `lotSz=minSz=1 contract`; Bybit `CAPUSDT` had `qtyStep=minOrderQty=10 CAP`
  and `minNotionalValue=5 USDT`. Near 0.05015 USDT/CAP, one matched contract
  is roughly $5/leg while two are just above the $10 intended cap. These
  fields and both books must be fetched again just before any real order;
  the snapshot is not a live trade approval. A $5 minimum on Bybit can also
  make one contract ineligible if the executable price falls below $0.05.
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
- The current durable-prewrite proof performs synchronous fsync **and full
  WAL replay on the event loop**. It is a correctness baseline, not a
  latency-qualified final path: cost grows with WAL history and can exceed
  the ~1 ms signal-to-send goal. Before live arming, benchmark p50/p99/p999
  on the target VPS with realistic WAL history, then use a separately
  reviewed bounded durable-ack/checkpoint design if this mode fails the
  latency gate. Never bypass durability merely to hit the latency target.

The isolated `validation.ev2_prewrite_no_order_benchmark` probe measures
this risk on the target host without credentials or network-capable sockets.
It reports the **exact opt-in engine fence** and signal-to-memory-`asend`
boundary on fresh WALs, then the production no-order audit with an
increasing WAL history. Readiness in the probe is synthetic, and its
memory boundary is not a real WebSocket write; results cannot qualify a
live release even if they meet the latency budget. Run it with
`python3 -m validation.ev2_prewrite_no_order_benchmark --samples 100
--history 300` from an isolated checkout, never from a running service.

### Target-VPS first result (2026-09-24)

Isolated checkout `/root/spread_ev2_13b2_bench` at `a64d897`, using
`/root/venv/bin/python` on `a845945761.local`. The probe used temporary
local WAL files and memory-only sockets; private readiness was simulated.
No trade socket, venue API, market quote or live order was involved. The
machine-readable result is
[`EV2-13B2-no-order-prewrite-vps-result.json`](EV2-13B2-no-order-prewrite-vps-result.json).

- Exact opt-in engine fence, fresh WAL, n=100: p50 **2.340 ms**, p99
  **3.524 ms**. Signal to first memory `asend`: p50 **4.542 ms**, p99
  **6.785 ms**. This is already above the ~1 ms signal-to-send objective
  *before* any network write.
- Existing no-order audit, n=300 with growing WAL to 511,746 bytes: total
  signal-to-audit-result p50 **583 ms**, p99 **2,042 ms**. First 75 attempts
  p50 73 ms; last 75 p50 1,628 ms. This path includes two fsynced events,
  thread hand-offs, audit work and full replay; it is **not** the same
  sample population as the exact single-accepted-event fence. The growth
  is evidence of a scaling problem but does not isolate its sole cause.
- `orders_sent=0`; collector-next and theta-k1 would-sent remained active
  with `NRestarts=0` at the post-run check.

Conclusion: the current synchronous full-replay pre-dispatch design is
not latency-qualified. Keep both live startup blocks. Next, instrument
fsync, replay and FSM fold separately; design a bounded durable-ack or
checkpoint verification protocol that retains crash safety, then repeat
this no-order target-VPS probe and only later a production-path canary.

## 5. Minimal patch / experiment plan

1. Complete and review EV2-12B/C/D live integration; preserve both current
   fail-closed gates until the production adapter is proven end to end.
   The adapter must hydrate engine state from the exact WAL replay before
   enabling durable prewrite; a mismatch intentionally blocks sending.
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
6. For each OPEN and CLOSE, freeze the accepted public L1 tick ring around
   the signal. Plot executable spread on a relative **millisecond** axis
   from local monotonic time, without interpolating missing ticks or pairing
   quotes older than the declared staleness limit. Mark the signal, each
   venue's local send and ACK, each private fill *receive*, and the
   exchange-reported fill time as a separate clock-domain marker with an
   explicit uncertainty/offset note. Partial fills get multiple markers;
   missing events stay missing. Do not reconstruct the curve from 1 Hz
   `would_sent` rows or infer fill from an ACK.

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
