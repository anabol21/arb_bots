# Execution v2: OKX/Bybit private contour

Status: proposed architecture, implementation not started

Date: 2026-09-17

Scope: track 3 / B-private and its glue to the existing `would_sent` strategy

## 1. Pipeline block

```text
public market data -> existing strategy block -> immutable TradeIntent
                                              |
                                              v  (same process + asyncio loop)
                                  ExecutionEngine / risk gate
                                    |                    |
                                    v                    v
                            warm Bybit trade WS    warm OKX trade WS
                                    |                    |
                                    +------ parallel ----+
                                              |
                              ACKs (request state only)
                                              |
                    private order/fill/position streams (authoritative)
                                              |
                              PositionProjection + recovery FSM
                                              |
                  local append-only WAL -> PostgreSQL projector
                                              |
                         Sentry exporter + metrics + MCP
                                              |
                         Codex / Cursor / three Grok bots
```

The production collector remains a separate service and frozen dependency.
The final trading runtime combines the existing strategy module and the new
executor in one process and one event loop. PostgreSQL, Sentry delivery, MCP,
reports and compaction stay outside the latency-critical path.

## 2. Existing files/modules involved

- `app/bot/runtime.py` and `app/bot/theta_trade_manager.py` own the current
  strategy/runtime glue and `would_sent` state.
- `app/bot/private/live_broker.py`, `ws_trivial_dual_leg.py`,
  `ws_warm_loop.py`, `ws_warm_session.py`, `ws_private.py` and
  `dual_leg_ack.py` contain the proven private/trade WebSocket pieces.
- `app/bot/private/journal_v1.py` is the starting point for the durable event
  ledger; `app/bot/sentry_setup.py` supplies the existing Sentry contract.
- `deploy/systemd/` contains templates only. A file in this directory never
  proves that the corresponding unit is installed or running on the VPS.

Observed gaps that v2 must close:

1. `LiveBroker` currently changes local position after two accepted trade
   ACKs. ACK means request acceptance, not fill.
2. The send path crosses queues, threads and event loops and polls for send
   timestamps. Clean samples are fast, but the architecture does not protect
   the p99 tail.
3. Signing, serialization and synchronous observability work still occur too
   close to the execution loop.
4. VPS `/root/spread_staging` is a copied tree without `.git`; deployed code
   cannot be mapped reliably to a reviewed commit.
5. The local `Desktop/spread` checkout and GitHub `main` have diverged. The
   local tree contains uncommitted/old private-contour files and must not be
   used as the new base.

## 3. Candidate designs and selected design

| Design | Latency | Isolation | Recovery complexity | Decision |
|---|---:|---:|---:|---|
| Same process and loop | Best | Medium | Lowest | **Selected** |
| Separate local process over UDS/shared memory | Good | High | Medium | Keep as fallback experiment |
| Network execution service | Worst for 1 ms SLO | Highest | Highest | Reject for v2 |

Selected persistence is local append-only WAL plus an asynchronous PostgreSQL
projection. Direct PostgreSQL writes are forbidden on signal-to-send. A local
WAL-only system remains a degraded mode: trading may continue only while the
WAL writer is healthy; MCP/history projection may lag.

Python is the first implementation. Rust is considered only after a target-VPS
profile shows that the selected Python topology cannot meet the latency SLO.
No rewrite is justified by local microbenchmarks alone.

## 4. Key risks and failure modes

### State authority

The exchange is authoritative for orders, fills and positions. Private streams
are the primary observation channel; signed read-only REST is the recovery and
reconciliation channel. WAL is the authoritative record of what this process
decided and observed. PostgreSQL and in-memory objects are projections.

An ACK may move a leg from `SENT` to `ACK_ACCEPTED` or `ACK_REJECTED`; it may
never create or remove a position. A spread is `OPEN` only after both legs are
confirmed by fill/position observations and their quantities match within the
instrument-lot tolerance.

### Per-leg and spread state machines

```text
leg:    NEW -> SENT -> ACK_ACCEPTED -> PARTIAL -> FILLED
                    \-> ACK_REJECTED
                    \-> UNKNOWN -> RECONCILING -> terminal state

spread: IDLE -> ARMED -> DISPATCHING -> EXPOSURE_UNKNOWN
          -> OPEN -> CLOSING -> FLAT
          -> RECOVERING -> FLAT | HALTED
```

`UNKNOWN`, stream generation changes, lost correlation, stale account data,
WAL failure or mismatched positions block every new open. Reduce-only recovery
and reconciliation remain allowed.

### One-leg execution

If one leg is filled and the peer is confirmed unfilled:

1. cancel a still-live peer order;
2. reconcile orders and positions on both venues by client intent id;
3. flatten the filled exposure on the functioning venue with reduce-only;
4. require private-stream plus REST evidence that both venues are flat;
5. latch `HALTED` if flatness cannot be proven.

The executor must not blindly resend after an ACK timeout and must not chase a
missing leg to preserve the trade. An ambiguous timeout enters reconciliation
before any further order action.

### Availability and backpressure

- Loss/staleness of either trade socket blocks opens.
- Loss/staleness of either private stream blocks opens until REST reseed and
  stream catch-up complete.
- A full critical-event queue or failed WAL blocks opens; non-critical metrics
  may be sampled, but lifecycle events may not be dropped.
- A restart begins in `RECONCILING`. Live opens remain disabled until WAL
  replay, open-order lookup, position lookup and private-stream readiness agree.
- Sentry, PostgreSQL, dashboards, MCP and chat outages never block closes or
  reconciliation and never run on the hot path.

### Existing Sentry compatibility

Existing event names/fingerprints remain available to the current Grok
automations. Capture and flush move to an asynchronous exporter fed by the WAL;
the event loop never performs a synchronous Sentry flush. Delivery lag and
exporter backlog become explicit health metrics.

## 5. Minimal implementation and agent plan

### Public contracts

`TradeIntent` is immutable and contains:

- `schema_version`, `intent_id`, `run_id` and `policy_version`;
- `signal_mono_ns` plus wall-clock timestamp for correlation only;
- coin, spread direction, open/close action and requested USDT notional;
- a monotonic expiry deadline and reference to the redacted signal snapshot;
- canary stage and risk-policy revision.

The executor resolves cached instrument metadata and creates two `LegPlan`
objects containing venue, instrument, side, quantity, reduce-only flag and a
deterministic venue-safe client id derived from `intent_id`. The same id is used
for ACK, fill, position, WAL and Sentry correlation. Secrets, signatures, venue
order ids and raw account payloads never enter Git, PostgreSQL or MCP.

`ExecutionEvent` is an append-only union covering intent acceptance/rejection,
socket writes, ACKs, partial/final fills, cancels, reconciliation, recovery
orders, flatness proof, pause state and faults. Every event has `event_id`,
`intent_id`, per-intent sequence, monotonic timestamp and redacted payload.

### Hot path

1. The strategy calls `ExecutionEngine.submit(intent)` on the same event loop.
2. The engine performs only cached checks: state is `READY`, no active intent,
   `K_live=1`, notional cap, intent TTL, metadata freshness, socket health,
   stream generation and kill-switch state.
3. Static frame fields are prebuilt. The hot path patches client id/timestamp,
   signs, serializes and schedules both venue writes without thread hops.
4. Signal time and the first underlying transport write for each venue use the
   same monotonic clock. The dual-leg latency is the slower of the two writes.
5. ACK/fill processing continues asynchronously. Disk, DB, Sentry, HTML and
   ordinary logging are not awaited by `submit`.

### Cursor-agent workflow

Codex owns architecture, task decomposition, review, evidence and release
gates. Code tasks run through Cursor headless agents using the existing exact
model slug `cursor-grok-4.6-high-fast`.

Each task uses an isolated worktree and branch, one focused diff and a task
packet containing objective, allowed paths, frozen paths, invariants, test
commands, expected evidence and an explicit ban on VPS mutations. Suggested
sequence:

1. state-machine/contracts agent;
2. single-loop WebSocket runtime agent;
3. WAL/projector/Sentry exporter agent;
4. recovery and one-leg-flatten agent;
5. MCP/control-plane agent;
6. independent validator/critic before integration.

Branches merge `feature -> dev -> canary -> main`. The existing `dev` branch
has no unique commits and is behind `main`; preserve its old pointer, then
fast-forward it to `main` before new work. No agent pushes directly to `main`.
Production deployment and promotion always require the user's confirmation.

## 6. VPS and storage validation plan

### Repository and release hygiene

1. Preserve `Desktop/spread` before touching it: a bundle of Git refs plus a
   local, non-uploaded manifest/archive of modified and untracked files.
2. Review the 11 local-only commits and untracked artifacts. Port only proven
   work in dedicated PRs; do not import the local `BotRunrutime` typo or replace
   current `main` with the old untracked `live_broker.py`.
3. Build immutable releases under a commit-SHA directory. A deployment manifest
   records Git SHA, artifact hashes, schema versions and test evidence.
4. systemd runs the immutable release path with secrets kept in root-readable
   environment files and runtime state under `/data`. Do not rsync over a live
   unversioned working tree.

### Runtime isolation

The new unit is separate from `spread-collector` and receives explicit CPU,
memory and restart limits. It initially runs as a shadow copy of the existing
strategy. The existing `spread-bbot-theta-k1-canary` remains the comparison
source until v2 passes shadow gates. At cutover, only one strategy/executor
runtime may own live order capability for the account.

PostgreSQL listens on loopback only. The projector may lag or restart without
stopping the execution state machine. WAL and Postgres are reconciled by
monotonic event sequence and idempotent upserts.

### MCP and pause path

The MCP service uses Streamable HTTP over a stable public HTTPS endpoint,
strong per-bot bearer/OAuth credentials, rate limits and redacted schemas.
Read tools never expose secrets or raw account identifiers. The only write tool
is idempotent `pause_canary(reason, idempotency_key)`:

- it latches “no new opens” through a local authenticated control socket;
- it does not resume, deploy, change limits or place/close arbitrary orders;
- closes and automatic one-leg recovery continue;
- every call is written to WAL/PostgreSQL with actor and reason.

## 7. Success criteria

### Correctness and safety

- ACK-only paths cannot create/remove local positions.
- Duplicate/replayed intents cannot create duplicate venue orders.
- Crash at every lifecycle boundary recovers to proven `OPEN` or proven `FLAT`,
  otherwise remains `HALTED` with no new opens.
- Partial fill, one-leg reject, ACK loss, stream reconnect and stale REST data
  are covered by deterministic tests.
- Every canary round-trip ends with both exchanges flat and no live orders.

### Latency

The contract is signal callback entry to the first underlying socket transport
write, measured separately for Bybit and OKX and as the slower dual-leg value.
On the target VPS under normal collector load, after warm-up:

- `p50 <= 1 ms`;
- `p99 <= 3 ms`;
- `p99.9 <= 10 ms` is an alert threshold;
- no negative/mixed-clock samples and no missing accepted-intent samples.

Percentile acceptance uses at least 10,000 no-order instrumented dispatches on
the real event loop. The small live canary validates correctness and detects
gross regression; it is not used to claim a statistically meaningful p99.

### Rollout

1. replay/fault-injection test suite green;
2. target-VPS no-order latency benchmark green;
3. shadow for at least 20 continuous hours, plus a separate deterministic
   replay of at least 300 eligible signals, with no collector
   regression, unknown state, dropped lifecycle event or MCP/Sentry divergence;
4. one bounded live round-trip at `$20` per leg and `K_live=1`;
5. twenty bounded live round-trips at the same cap;
6. explicit user approval before production promotion.

## 8. Recommended next step

Land this documentation-only RFC, then run two independent tasks in order:

1. preserve/classify the divergent local checkout and normalize `dev` from the
   reviewed GitHub `main`;
2. implement only the typed intent/event contracts and fill-authoritative state
   machine with simulations and fault injection—no live send-path change yet.

The WebSocket hot-path refactor starts only after the state-machine tests make
ACK/fill/partial/recovery semantics executable.

Tooling references: [Cursor CLI](https://docs.cursor.com/en/cli/overview) and
[Cursor ACP](https://docs.cursor.com/en/cli/acp).
