# EV2-04 task packet: private event adapters and position projection

Owner: Cursor runtime agent

Model: `cursor-grok-4.6-high-fast`

Base branch: `codex/execution-v2-dev-2026-09-18`

Reviewed predecessor: `41ed2a7` (EV2-03 single-loop transport, critic `PASS`)

Date: 2026-09-19

VPS/live authority: none

## Objective

Implement the pure execution-v2 ingestion layer that converts decoded OKX and
Bybit trade ACK, order/execution, position and complete REST-reconciliation
snapshots into EV2 `ExecutionEvent` values. It must preserve cumulative fill
quantities, exact deterministic-client-id correlation, stream-generation
freshness and replay determinism without exposing raw venue frames or venue
order/account identifiers.

This patch does not read sockets, perform REST calls, own reconnects, write a
WAL, mutate `SpreadState`, send orders or wire the current runtime. Existing
private modules remain behaviorally unchanged.

Read first:

- `AGENTS.md` in full;
- `docs/execution-v2-architecture.md`;
- `docs/execution-v2-canary-roadmap.md`, especially EV2-04;
- EV2-02 and EV2-03 task packets;
- `app/bot/execution/contracts.py` and `state_machine.py`;
- parsing/correlation evidence in `app/bot/private/ws_private.py`;
- venue frame shapes in `app/bot/private/wire_transcript.py`,
  `ws_messages.py` and the focused private tests.

## Pipeline block

```text
decoded venue mapping + receive monotonic time + stream generation
                            |
                            v
            exact client-id / instrument correlation registry
                            |
                            v
       Bybit/OKX ACK, order, execution, position, REST adapters
                            |
                 generation + dedupe fence
                            |
                            v
          ordered immutable ExecutionEvent tuple + redacted issues
                            |
                            v
                   EV2-02 apply_events replay
```

## Existing gap being closed

The current private parser is useful historical evidence but cannot be reused
as the EV2 authority:

- it processes only the first row in a venue frame;
- `ParsedStreamEvent` carries terminal category but no cumulative fill size;
- `partiallyfilled` is treated as working state rather than quantity evidence;
- the current REST reseed is categorical and cannot prove per-leg zero
  position plus zero open orders;
- its correlation fingerprints belong to journal v1 rather than EV2
  deterministic client ids.

Do not patch these limitations into the legacy runtime in EV2-04. Build the
new adapter layer next to EV2-02/03 and leave cutover for later patches.

## Allowed changes

- add `app/bot/execution/adapters.py`;
- amend `app/bot/execution/__init__.py` only to export the reviewed EV2-04 API;
- add `tests/test_execution_adapters.py`;
- amend this task packet only to append implementation evidence.

Everything else is frozen, including `contracts.py`, `state_machine.py`, all
existing `app/bot/private/**` modules, runtime/strategy code, collector code,
deploy/systemd, secrets and VPS paths.

If the existing EV2 contracts cannot represent a required safe observation,
stop and report the exact contract gap. Do not widen the diff silently.

## Required design

Use stdlib only. Public value objects are immutable. Mutable sequencing and
dedupe state, if used, must be explicit, bounded to registered active intents
and reconstructible from a supplied last sequence/generation.

### Correlation registry

- Register one `TradeIntent` with exactly two `LegPlan` values, one per venue.
- Index only EV2-02 deterministic `client_id` plus the unique
  `(venue, instrument)` position key. Never index venue order ids.
- Registration validates matching `intent_id`, `run_id`, derived client ids
  and no ambiguous duplicate venue/instrument binding.
- Restart construction accepts the last applied per-intent sequence and last
  emitted monotonic timestamp; first new event continues from those values.
- Unknown client ids remain unknown. Never manufacture an intent, leg or
  third leg from a venue payload.

### Input and output boundary

- Accept already JSON-decoded mappings plus explicit venue, channel/source,
  reconnect generation and local receive monotonic nanoseconds.
- Never use venue wall timestamps or venue sequence/timestamp fields as EV2
  monotonic ordering. Late/reordered venue evidence uses its later local
  receive time.
- Process every valid row in multi-row frames; never silently take row zero.
- Return an immutable batch containing zero or more `ExecutionEvent` values
  and zero or more redacted adapter issues. Public views/reprs must omit raw
  payloads, API material, account ids, venue order ids and source identifiers.
- Event ids are deterministic and venue-safe. Exact duplicate evidence must
  not consume another sequence or produce a conflicting event.

### ACK semantics

- Bybit `reqId` and OKX `id` correlate exactly to the registered EV2 client
  id. Success emits `ACK_ACCEPTED`; venue/per-order failure emits
  `ACK_REJECTED` with allowlisted `venue_rejected`.
- For OKX, per-row `sCode` failure overrides top-level success.
- An id-less OKX error is not correlated implicitly. It may be adapted only
  when the caller supplies the exact expected registered client id for the
  one outstanding request; otherwise return a redacted unknown-correlation
  issue and no ACK event.
- ACK never emits fill, position, `OPEN`, `FLAT` or reconciliation evidence.

### Fill/order semantics

- Bybit uses `orderLinkId`; prefer cumulative `cumExecQty`. OKX uses
  `clOrdId`; prefer cumulative `accFillSz`. Accept documented equivalent
  cumulative fields only when covered by fixtures.
- Emit `PARTIAL_FILL` for positive cumulative quantity below the registered
  plan quantity and `FILL` when the venue reports terminal filled or the
  cumulative quantity reaches/exceeds the plan. Preserve the observed
  cumulative quantity, including overfill, so EV2-02 can fail closed.
- Per-fill-only rows may be accumulated only when a stable source identifier
  is available for internal hashed dedupe. The raw source id is never exposed.
  Without cumulative quantity or stable dedupe, return an issue instead of
  guessing.
- Duplicate/replayed order and execution rows, including the same fill seen on
  two channels, emit at most one quantity event and do not advance sequence.
- A later row with lower cumulative fill is stale evidence: do not regress
  quantity; report it as a redacted stale issue.
- Cancel/reject order states must not erase a previously observed fill. Map a
  correlated cancellation to `CANCEL_ACK` only when the frame proves that
  lifecycle fact; leave recovery interpretation to EV2-02.

### Position and open-order projection

- Position correlation uses the unique registered `(venue, instrument)` key,
  not an order id. Group every returned row for that instrument. Exactly one
  nonzero venue side compatible with the registered `LegPlan` may project its
  canonical absolute base quantity. Simultaneous/conflicting nonzero hedge
  sides are ambiguous and must block matched reconciliation; do not sum them
  into a seemingly valid hedge.
- Emit `POSITION_OBSERVED` for explicit venue position rows, including an
  explicit zero. A missing stream row never means zero.
- A complete REST snapshot may synthesize zero position for a registered leg
  only when the caller explicitly marks the venue snapshot complete and the
  instrument is absent from the full returned position set.
- A complete REST open-order snapshot emits `OPEN_ORDERS_OBSERVED` with the
  count of **all** open orders for the registered instrument, including unknown
  client ids. Unknown-client rows also produce a redacted correlation issue;
  they can never be hidden to manufacture flatness. Absence means zero only
  for a complete successful full snapshot.
- Incomplete, malformed, paginated-with-more, failed or generation-stale REST
  data never emits false zeros and never emits matched reconciliation.
- After a complete current-generation REST snapshot emits both position and
  open-order observations for every registered leg on that venue, emit a
  venue/leg-scoped `RECONCILIATION` with `matched=true`. Otherwise emit a
  redacted issue or `matched=false`; never claim flatness directly.

### Generation and sequencing fence

- The adapter is initialized with the current expected generation per venue.
- A lower generation is stale: drop it with an issue and no lifecycle event.
- A higher/unannounced generation emits one
  `STREAM_GENERATION_MISMATCH` per affected registered intent, updates the
  expected generation and blocks ordinary evidence for that venue until a
  complete current-generation REST snapshot is adapted.
- Repeated notification of the same mismatch is idempotent.
- Fresh REST reconciliation re-enables ordinary evidence only after its
  observation events and matched reconciliation event have been emitted.
- Per-intent sequences are contiguous in adapter emission order. Duplicate or
  dropped stale source evidence does not consume a sequence.

## Locked safety semantics

- Venue data is evidence, not an instruction to place, retry, cancel or flatten.
- ACK-only input can never create position authority.
- Truly unknown correlation never attaches to the sole active intent merely
  because `K_live=1`.
- If an unknown client id references an instrument that unambiguously belongs
  to a registered active leg, the adapter may emit `UNKNOWN_CORRELATION` for
  that known leg without copying the unknown id. Ambiguous/no-leg cases remain
  redacted issues for the later engine-level global fault gate.
- Floats, NaN/Infinity, negative fills, malformed decimals, missing required
  keys and conflicting venue/instrument/client-id combinations fail closed.
- No raw mapping, key, signature, account value, venue order id or execution id
  may appear in an event, exception, repr or public serialization.
- Adapter import/construction performs no network, file, env, socket, thread,
  task, journal or live-order action.

## Tests and acceptance evidence

At minimum cover:

1. Bybit ACK accept/reject and ACK-only replay never opens;
2. OKX top-level code plus per-row `sCode`, exact id and id-less-error context;
3. Bybit partial-to-full cumulative fill and duplicate lower/stale quantities;
4. OKX partial-to-full `accFillSz`, overfill preserved;
5. every row in multi-row order/execution frames is processed;
6. cross-channel duplicate cumulative fill is emitted once;
7. position mapping for buy/sell/net rows and explicit zero;
8. complete REST positions/open-orders snapshot, explicit absence-to-zero and
   matched reconciliation ordering;
9. incomplete/paginated/failed REST snapshot produces no false zero/match;
10. stale generation drop, one mismatch on generation advance, blocked venue
    evidence and successful current-generation REST reseed;
11. duplicate frame replay preserves next sequence and deterministic ids;
12. restart seeded with prior sequence/monotonic time continues contiguously;
13. unknown and ambiguous correlation never creates a third leg;
14. malformed/float/NaN/negative quantities fail closed and redact input;
15. deterministic adapter events replay through EV2-02 `apply_events` for
    ACK-before-fill, fill-before-ACK, partial/final and reconnect paths;
16. public result/issue/error views contain no raw or forbidden fields;
17. importing/constructing the adapter performs no I/O or live action.

Run:

```text
PYTHONPYCACHEPREFIX=./.pycache python3 -m py_compile \
  app/bot/execution/adapters.py tests/test_execution_adapters.py

PYTHONPYCACHEPREFIX=./.pycache python3 -m unittest \
  tests.test_execution_adapters \
  tests.test_execution_transport \
  tests.test_execution_contracts \
  tests.test_execution_state_machine -v

PYTHONPYCACHEPREFIX=./.pycache python3 -m unittest \
  tests.test_warm_single_loop \
  tests.test_warm_ws_place_threadsafe \
  tests.test_dual_leg_ack \
  tests.test_wire_transcript \
  tests.test_sentry_integration -v

python3 -m pytest tests/test_order_lease_sol_close.py -v

git diff --check
```

Report exact test counts and the eight blocks required by `AGENTS.md`.

## Stop conditions

Stop and report instead of widening scope if:

- safe cumulative quantity cannot be derived from the fixture without
  guessing or storing a forbidden raw identifier;
- exact correlation requires a venue order/account id;
- a required event cannot be represented by the frozen EV2-02 contract;
- REST completeness cannot be established explicitly at the input boundary;
- implementing the adapter would require editing legacy private parsing,
  current live wiring or reconnect behavior;
- a test would require real exchange connectivity, credentials or VPS access.

Leave changes uncommitted for Codex review and an independent Cursor critic.
Do not push, access the VPS or start any canary.

## Implementation evidence (2026-09-19)

Local worktree only. No VPS access, no live credentials, no commit, no push.
No network services and no live exchanges.

Added / amended (allowed paths only):

- `app/bot/execution/adapters.py` (`bbot.execution.adapters.v1`)
- `app/bot/execution/__init__.py` (EV2-04 public API export only)
- `tests/test_execution_adapters.py`
- this task packet (evidence appendix only)

Existing private modules, `contracts.py`, `state_machine.py`, live broker,
runtime, collector, systemd and VPS paths were not edited. Stop conditions
were not reached: cumulative quantity uses `cumExecQty` / `accFillSz` or
internally hashed per-fill ids; correlation uses only EV2 client ids and
`(venue, instrument)`; REST completeness is an explicit caller flag plus
payload failure/pagination markers; all required observations fit the
frozen EV2-02 event types.

Locked in this adapter:

- One registered `TradeIntent` with exactly two `LegPlan` values; index
  only derived `client_id` and unique `(venue, instrument)`.
- Local receive monotonic time is the only EV2 clock; every valid row in
  a multi-row frame is processed.
- Bybit `reqId` / OKX `id` ACK accept or `venue_rejected`; OKX per-row
  `sCode` overrides top-level success; id-less OKX errors require the
  exact expected registered client id.
- Cumulative partial/final fills, overfill preserved, stale lower qty
  dropped, cross-channel duplicates emit once.
- Stream positions never synthesize zero; complete current-generation
  REST may; unknown-client open orders are counted and cannot hide
  flatness; matched recon is venue/leg-scoped.
- Lower generation is stale; higher generation emits one
  `STREAM_GENERATION_MISMATCH` per intent, blocks ordinary evidence, and
  re-enables only after complete REST observations plus matched recon.
- Duplicate evidence does not consume sequence. Restart continues from
  seeded last sequence/monotonic time.
- Public views omit raw frames, secrets, account ids and venue order ids.
  Import/construction performs no I/O.

Commands and counts (local Python 3.9.6):

```text
PYTHONPYCACHEPREFIX=./.pycache python3 -m py_compile \
  app/bot/execution/adapters.py tests/test_execution_adapters.py
# pass

PYTHONPYCACHEPREFIX=./.pycache python3 -m unittest \
  tests.test_execution_adapters \
  tests.test_execution_transport \
  tests.test_execution_contracts \
  tests.test_execution_state_machine -v
# 159 tests, OK (adapters 20, transport 27, contracts+state machine 112)

PYTHONPYCACHEPREFIX=./.pycache python3 -m unittest \
  tests.test_warm_single_loop \
  tests.test_warm_ws_place_threadsafe \
  tests.test_dual_leg_ack \
  tests.test_wire_transcript \
  tests.test_sentry_integration -v
# 57 tests, OK (warm loop 8, warm place threadsafe 18, ACK 13,
# wire transcript 8, Sentry 10)

python3 -m pytest tests/test_order_lease_sol_close.py -v
# 3 passed

git diff --check
# pass (files left uncommitted for independent review)
```

New EV2-04 adapter tests: 20.
EV2-03 transport tests: 27 preserved.
EV2-02 contracts/state machine: 112 preserved.
Private warm/ACK/wire/Sentry regression: 57.
Lease close pytest: 3.
Combined: 219.

## Critic-blocker repair evidence (2026-09-19)

Local worktree only. No VPS access, no live credentials, no commit, no push.
No sockets, secrets, collector, or frozen `contracts.py` / `state_machine.py`
edits. `__init__.py` was not amended in this repair.

Independent-review blockers locked in `adapters.py`:

1. Non-mapping rows still process sibling valid rows on ordinary stream
   frames. On a complete REST snapshot they invalidate the whole snapshot:
   no synthesized zero, no `matched=true`.
2. REST position rows missing instrument fail closed (`malformed_frame`).
   Absence-to-zero is not synthesized from an unidentifiable row.
3. REST open-order rows missing instrument fail closed. They are not counted
   as zero and cannot emit `matched=true`. Unknown-client rows that do have
   an instrument remain counted and still block match.
4. `_resolve_bound` rejects a client id bound to the other venue even when
   instrument is missing or foreign. No event is emitted for the wrong leg.
5. Explicit Bybit `retCode` not in `{0,"0"}` overrides `success=true` and
   emits `ACK_REJECTED` / `venue_rejected`.
6. Each REST half is replaced on a new snapshot. Failed, incomplete,
   paginated, stale-generation, malformed, or conflicting snapshots break
   the current pair so leftover/stale halves cannot emit `matched=true`.
   A later clean complete current-generation positions+orders pair clears
   that dirty state, emits `matched=true`, and unblocks.

Critic residuals:

- OKX execution row missing `state`: **fixed**. The packet emits `FILL` only
  when the venue reports terminal filled or cumulative quantity
  reaches/exceeds plan. Inventing `state=filled` on a fills row made a
  sub-plan cumulative look terminal. Missing state is now empty; `FILL` vs
  `PARTIAL_FILL` follows observed quantity only.
- Seeded restart duplicate behavior: **not fixed**. Restart seed is only
  last per-intent sequence, last monotonic time, and expected generation.
  The packet requires dedupe state to be reconstructible from that seed.
  The in-memory emitted-key / last-fill maps cannot be rebuilt from those
  scalars without guessing. Widening the constructor to accept a dedupe
  snapshot would expand the reviewed API. Treating the first post-restart
  frame as a duplicate would drop real first evidence and weaken
  fail-closed observation. Same-instance replay still does not consume
  sequence; seeded restart still continues contiguously. Exact
  cross-process frame idempotency belongs to later WAL replay (EV2-05).

Commands and counts (local Python 3.9.6):

```text
PYTHONPYCACHEPREFIX=./.pycache python3 -m py_compile \
  app/bot/execution/adapters.py tests/test_execution_adapters.py
# pass

PYTHONPYCACHEPREFIX=./.pycache python3 -m unittest \
  tests.test_execution_adapters \
  tests.test_execution_transport \
  tests.test_execution_contracts \
  tests.test_execution_state_machine -v
# 166 tests, OK (adapters 27, transport 27, contracts+state machine 112)

PYTHONPYCACHEPREFIX=./.pycache python3 -m unittest \
  tests.test_warm_single_loop \
  tests.test_warm_ws_place_threadsafe \
  tests.test_dual_leg_ack \
  tests.test_wire_transcript \
  tests.test_sentry_integration -v
# 57 tests, OK (warm loop 8, warm place threadsafe 18, ACK 13,
# wire transcript 8, Sentry 10)

python3 -m pytest tests/test_order_lease_sol_close.py -v
# 3 passed

git diff --check
# pass (files left uncommitted)
```

New EV2-04 adapter tests: 27 (20 original + 7 critic-blocker/residual cases).
EV2-03 transport tests: 27 preserved.
EV2-02 contracts/state machine: 112 preserved.
Private warm/ACK/wire/Sentry regression: 57.
Lease close pytest: 3.
Combined: 226.

## Same-qty REST observation accounting (2026-09-19)

Local worktree only. No VPS access, no live credentials, no commit, no push.
No sockets, secrets, collector, or frozen `contracts.py` / `state_machine.py`
edits. `__init__.py` was not amended in this repair.

Remaining critic blocker:

- `_adapt_rest_positions` resets `observed_position_legs` on every snapshot.
  A valid explicit same-quantity retry then hit `_make_event` dedupe for
  `POSITION_OBSERVED`, so the leg was not re-added. After a failed or
  incomplete open-orders half, a later clean same-generation
  positions+orders pair never emitted `matched=true` and never unblocked.

Locked:

- Validated explicit REST position accounting is independent of whether
  duplicate `POSITION_OBSERVED` emission is suppressed.
- Conflicting or malformed position rows still do not count.
- Same-generation same-qty retry does not consume sequence.
- Adversarial path: higher-generation mismatch → explicit nonzero
  position → failed then incomplete orders → same explicit qty retry →
  clean orders → `matched=true`, adapter unblocked, no second position
  event.

Commands and counts (local Python 3.9.6):

```text
PYTHONPYCACHEPREFIX=./.pycache python3 -m py_compile \
  app/bot/execution/adapters.py tests/test_execution_adapters.py
# pass

PYTHONPYCACHEPREFIX=./.pycache python3 -m unittest \
  tests.test_execution_adapters \
  tests.test_execution_transport \
  tests.test_execution_contracts \
  tests.test_execution_state_machine -v
# 167 tests, OK (adapters 28, transport 27, contracts+state machine 112)

git diff --check
# pass (files left uncommitted)
```

New EV2-04 adapter tests: 28 (20 original + 7 prior critic cases + 1
same-qty REST retry).
EV2-03 transport tests: 27 preserved.
EV2-02 contracts/state machine: 112 preserved.
Adapter + EV2 combined: 167.

## Independent critic acceptance (2026-09-19)

Final read-only Cursor critic verdict: `PASS`.

The critic independently replayed the repaired reconnect path: generation
mismatch, explicit nonzero REST position, failed and incomplete open-orders
halves, same-generation same-quantity position retry, then a clean open-orders
half. The retry consumed no sequence, the clean pair emitted
`RECONCILIATION matched=true`, and the venue fence unblocked. Malformed and
conflicting rows did not count as observations.

The critic also rechecked the six earlier blockers: malformed REST rows and
missing instruments cannot synthesize zero or matched reconciliation;
unknown-client open orders remain visible; cross-venue client ids cannot bind
the wrong leg; explicit Bybit `retCode` failure overrides `success=true`; and
a later clean full REST pair can recover a previously dirty fence.

Acceptance evidence remained local-only: 167 EV2 tests passed (28 adapters,
27 transport, 112 contracts/state machine), 57 private regression tests
passed, 3 lease tests passed, and `git diff --check` passed. No VPS, live
venue, credentials, runtime wiring or canary authority was used.
