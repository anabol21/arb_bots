# Execution v2: roadmap to the 30-coin canary

Status: development plan

Date: 2026-09-18

Development branch: `codex/execution-v2-dev-2026-09-18`

Reviewed base:

- `d5b6553` — execution-v2 architecture and agent handoff;
- `ce66638` — immutable contracts and fill-authoritative state machine
  (EV2-02, independent review `PASS`).

This branch is the linear integration line for the work started on 2026-09-18.
Every EV2 task is developed in an isolated Cursor worktree, independently
reviewed, and then committed or cherry-picked here. Nothing from this branch
is deployed merely because it exists in Git.

## Locked canary scope

The first execution-v2 canary uses the same 30-coin universe and frozen Gear
2.2 policy as the existing `would_sent` contour:

```text
KAITO,HOME,WAL,RVN,ONT,2Z,BICO,HMSTR,CAP,BLEND,EDEN,KMNO,GPS,ME,ZBT,
MOVE,COAI,AZTEC,APR,YB,ICX,AT,H,MUBARAK,ACU,LA,BEAT,PARTI,SIGN,GIGGLE
```

Locked initial risk/policy settings:

- global `K_live=1` across all 30 coins;
- `$20 USDT` notional per venue leg;
- `theta_open=0.50`;
- `p50_open=0.60`;
- `min_profit_pp=0.20`;
- `min_theta_close=0.05`;
- `fee_round_trip_pp=0.30`;
- one live executor owns the account; the existing `would_sent` unit stays
  read-only/stub and is the parity reference;
- ACK never creates or clears a position; venue fills/positions are authority;
- pause blocks new opens but never blocks close/recovery.

Changing the universe, knobs, notional, `K_live`, or strategy formula is not an
execution-v2 implementation change. It requires a separate reviewed policy
patch and a new canary declaration.

## Patch sequence

### EV2-03 — single-loop transport kernel

Build the non-owning execution transport on top of the existing warm OKX and
Bybit trade/private sockets.

Deliverables:

- one asyncio-loop API for parallel dual-venue transport writes;
- cached instrument metadata and OKX `instIdCode` outside the signal path;
- deterministic client ids from EV2-02 contracts;
- prebuilt static frame fields, with only timestamp/client id/signature patched
  at dispatch;
- monotonic markers at signal entry and the first underlying socket write;
- ACK receipt is asynchronous and never serializes the second venue send;
- fake-socket transcript, cancellation, reconnect and loop-ownership tests.

Gate: no runtime/strategy wiring and no live orders. The existing private
modules remain available until the new engine is proven.

### EV2-04 — private event adapters and position projection

Translate OKX/Bybit private `orders`, fills, positions, trade ACKs and REST
reseed results into `ExecutionEvent` without raw frames or venue order ids.

Deliverables:

- exact correlation by deterministic client id and intent id;
- cumulative partial/final fill quantities;
- per-stream generation and stale-evidence boundaries;
- normalized position/open-order observations;
- replayable folding into the EV2-02 state machine;
- fixture coverage for reconnect, duplicate, reordered and late events.

Gate: ACK-only fixtures never produce `OPEN`; an unknown event cannot create a
third leg or clear a recovery latch.

### EV2-05 — WAL v2, replay, projector and async exporters

Close the known `bbot.private.journal.v1` gap for fill quantities and partial
fills.

Deliverables:

- append-only `bbot.execution.wal.v2` records for every accepted decision and
  observed lifecycle event;
- bounded queue/backpressure and an explicit unhealthy-WAL open gate;
- deterministic restart replay followed by mandatory venue reconciliation;
- idempotent PostgreSQL projection outside the hot path;
- asynchronous Sentry/metrics export preserving the existing Grok-facing
  event names/fingerprints where compatible;
- crash-injection tests at every intent/send/ACK/fill/reconcile boundary.

Gate: restart reaches proven `OPEN` or proven `FLAT`; otherwise it remains
blocked/recovering. No lifecycle event is silently dropped.

### EV2-06 — ExecutionEngine and risk gate

Compose contracts, state machine, transport, private projection and WAL behind
one engine API.

Deliverables:

- cached hot-path checks: state, global `K_live=1`, intent TTL, coin allowlist,
  notional, metadata freshness, socket/private-stream readiness, pause and
  kill-switch state;
- parallel open/close dispatch with no disk, PostgreSQL, Sentry, HTML or normal
  logging await on signal-to-send;
- a single account-level slot derived from EV2 state, not ACK-based local
  position mutation;
- explicit ownership fencing so old and new live executors cannot both send;
- deterministic responses for accepted, rejected and recovery-required
  intents.

Gate: concurrency/fault tests prove that simultaneous signals from different
coins cannot exceed `K_live=1`.

### EV2-07 — recovery and restart orchestration

Make every non-happy lifecycle reachable and bounded.

Deliverables:

- cancel/reconcile/flatten flow for one filled leg and a confirmed-unfilled or
  timed-out peer;
- reduce-only recovery that remains available while opens are blocked;
- reconnect and REST reseed orchestration with post-uncertainty snapshots;
- restart recovery from WAL plus live venue state;
- sticky `HALTED` when flatness cannot be proven;
- operator-visible reason codes without arbitrary order-control endpoints.

Gate: fault matrix covers reject, timeout, partial, overfill, stale stream,
process crash, late fill and failed flatten. Every successful recovery ends
with both venue positions and open-order sets proven flat.

### EV2-08 — Gear 2.2 strategy bridge for the 30-coin universe

Replace the ACK-authoritative role of `LiveBroker` without changing the frozen
strategy.

Deliverables:

- `ThetaTradeManager` emits immutable `TradeIntent` at the existing decision
  point;
- open `trade_id == intent_id`; close uses a new intent id while retaining the
  same trade id;
- the existing 30-coin books/floor/TW-p50/theta observers remain the signal
  source;
- slot state comes from `ExecutionEngine` fill/position projection;
- would-send and execution-v2 rows share stable correlation fields;
- the old `gear22_would_send` unit and its data root remain unchanged.

Gate: deterministic replay produces identical eligible signals and decisions
between `would_sent` and execution v2; differences are classified rather than
silently ignored.

### EV2-09 — shadow parity and latency qualification

Run the complete 30-coin pipeline with order capability physically disabled.

Deliverables:

- target-VPS shadow using the same event loop and 30-coin workload;
- at least 20 continuous hours with no dropped lifecycle event, unknown state,
  collector regression or decision divergence; live eligible-signal count is
  reported but is not a wall-clock acceptance minimum because the strategy may
  produce no signal for a day;
- deterministic historical replay covers at least 300 eligible signals with
  exact ``would_sent`` / execution-v2 decision parity;
- at least 10,000 no-order instrumented dispatches after warm-up;
- latency report for both venues and the slower dual-leg write;
- CPU, memory, event-loop lag, queue depth and reconnect evidence.

Gate:

- `p50 <= 1 ms` signal callback to first transport write;
- `p99 <= 3 ms`;
- `p99.9 <= 10 ms` is the alert boundary;
- no negative/mixed-clock samples and no missing accepted-intent samples.

The shadow run validates parity and latency mechanics, not profitability.

#### EV2-09C — dual-readiness fence

Protect every normal two-venue dispatch with one immutable readiness lease.
Both trade sockets and both authenticated private streams must be ready; any
disconnect or venue-generation change invalidates the lease. The engine checks
the lease once at admission and again after frame finalization immediately
before both same-loop writes are scheduled, with no await between that final
check and the two `create_task` calls.

OPEN fails closed without changing lifecycle state. A normal strategy CLOSE
that loses readiness performs zero dual-leg writes and moves into mandatory
reconciliation/recovery; pause and kill-switch changes do not block close or
reduce-only recovery. Request-sent evidence retains the generation frozen in
the dispatch lease rather than reading a possibly newer post-send generation.

#### EV2-09D — readiness wiring and disconnect fault matrix

Wire private auth/subscription/reseed and trade-socket state from both venue
runtimes into the dual-readiness fence. Reconnect publication must first set
the affected component false, advance its venue generation, and only return to
ready after the required auth/subscription/reseed proof. Fault injection covers
disconnect before admission, during preparation, immediately before dispatch,
after one/both writes have started, and during recovery.

Gate: no normal dual-leg write starts while either venue is unready; a stale
lease cannot become valid again merely because all booleans return to true.

### EV2-10 — synthetic-signal readiness canary

An explicit no-order policy mode may replace rare alpha eligibility with one
replayable 1..100 roll per second across the whole 30-coin universe: `17`
opens while flat and `32` closes the held position. This mode must retain all
book/size/K=1/FSM gates, carry a distinct policy id, fail closed under any
live-order configuration and terminate only in the shadow `NullTradeSink`.
It is test coverage for manager lifecycle and reconnect/readiness experiments,
not strategy or profitability evidence.

Run it for an initial two-hour target-VPS window with a separate unit, data
root, log root and run id. Order capability remains physically absent. The run
must exercise repeated OPEN/CLOSE lifecycle cycles plus controlled private
disconnect/reconnect cases through the EV2-09C/09D readiness path.

### EV2-11 — restart-safe trade state recovery

Persist each committed synthetic or live open/close lifecycle before publishing
the K=1 slot state. On restart, strictly replay the trade journal into the
manager and shadow FSM. For live send, require signed read-only position and
open-order reconciliation on both venues before enabling decisions. Ambiguous
or mismatched exposure blocks new sends; it is never inferred as flat.

Qualify this with a separate two-hour no-order restart canary: commit a
synthetic OPEN, restart only its isolated unit, prove the same `trade_id` is
restored, then observe a CLOSE on the new process. Preserve the collector and
the continuing `would_sent` contour.

### EV2-12 — immutable canary release and control plane

Before preparing the immutable release, complete the EV2-12A–C execution
hardening gates in
[`EV2-12-to-gear22-private-prod-bridge.md`](task-packets/EV2-12-to-gear22-private-prod-bridge.md):
ACK-versus-fill exposure semantics, durable live adapter/WAL integration, and
quantity-aware two-venue reconciliation. EV2-11's no-order restart proof does
not certify a fillable live CLOSE or a flat venue position. The release and
control-plane work below is EV2-12D.

EV2-12A1 has a narrow [synthetic exposure fence and legacy live-start block](task-packets/EV2-12A-synthetic-exposure-fence.md). It does not complete
EV2-12A or authorize live orders.
EV2-12A2 adds the [fill-authoritative manager publication contract](task-packets/EV2-12A2-manager-exposure-projection.md);
it remains pure until EV2-12B provides durable runtime wiring.
EV2-12B1 adds the [fsynced WAL candidate fence](task-packets/EV2-12B1-durable-manager-candidate.md):
queue acceptance cannot publish K=1 exposure, and exact durable replay is
required before even proposing a lifecycle row. B2 venue reconciliation,
manager-journal fsync, runtime wiring and fault injection are still open;
the live-start block remains in place.

Prepare a reviewable release without starting it.

Deliverables:

- commit-SHA release directory and manifest with artifact/test hashes;
- isolated systemd unit, data root, log root and mode-600 secret file;
- startup preflight proving account ownership, universe hash, warm state,
  private-stream reseed, WAL health and `K_live=1`;
- read-only status/MCP surfaces and one idempotent `pause_canary` write action;
- compatibility events for the collector/infrastructure Grok bot, the
  `would_sent` bot and the private-contour bot;
- development ledger containing EV2 commits, experiments, canary stage and
  unresolved risks, so Grok automation cannot diverge from this branch.

Gate: deployment dry-run and rollback rehearsal succeed. Creating the unit
file does not authorize installing, enabling or starting it.

### EV2-13 — bounded live canary ladder

The detailed EV2-13A–D gates and EV2-14 production promotion are defined in
[`EV2-12-to-gear22-private-prod-bridge.md`](task-packets/EV2-12-to-gear22-private-prod-bridge.md).
In particular, dual order ACK is not a filled/flat proof; each live stage
requires venue-confirmed per-leg exposure and independent reconciliation.

Live steps require explicit user approval at every stage:

1. preflight with live credentials but order sends disabled;
2. an isolated one-round-trip real-order experiment at `$20/leg`, outside the
   continuing contour;
3. independent filled-quantity and flatness proof on both venues, with full
   WAL/Sentry/Grok parity;
4. a bounded live synthetic-policy contour, still global `K_live=1`, with an
   explicit round-trip cap and stop conditions;
5. only then enable the frozen real strategy in the continuing 30-coin canary.

Any unknown exposure, late uncorrelated fill, failed WAL, missing private
stream, ownership conflict, latency gate failure or collector regression stops
new opens. Close/reduce-only recovery remains allowed. The small live ladder is
a correctness gate; it is not used to claim a statistically meaningful p99.

## Integration and review discipline

- Each EV2 patch gets a task packet with allowed/frozen paths and exact tests.
- Cursor implementation agents use isolated worktrees based on
  `codex/execution-v2-dev-2026-09-18`.
- Codex reviews the diff and test evidence; a separate Cursor critic must
  return `PASS` before integration.
- Reviewed commits land linearly on this branch. No agent pushes `main`.
- `dev`, canary and production promotion are separate user-approved actions.
- VPS inspection is read-only until the release/canary stage explicitly grants
  mutation authority.

## Canary-ready definition

The branch is ready to request a live canary only when EV2-03 through EV2-12
are reviewed and green, the 30-coin universe and frozen strategy manifest
match the existing `would_sent` contour, the shadow/latency gates pass on the
target VPS, recovery fault injection is green, and the Grok/MCP handoff can
report the exact Git SHA and current execution state.
