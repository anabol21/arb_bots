# EV2-05 task packet: WAL v2, replay, projector and async exporters

Owner: Cursor runtime agent

Model: `cursor-grok-4.6-high-fast`

Base branch: `codex/execution-v2-dev-2026-09-18`

Reviewed predecessor: `b13bc62` (EV2-04 private adapters, critic `PASS`)

Date: 2026-09-19

VPS/live authority: none

## Objective

Implement the pure execution-v2 durable ledger kernel around the frozen EV2
contracts and state machine. The patch must persist every accepted
`ExecutionEvent`, including cumulative partial/final fill quantities, as an
append-only hash-chained `bbot.execution.wal.v2` record; deterministically
replay the durable prefix; expose an explicit unhealthy-WAL open gate; provide
an idempotent in-memory projection boundary; and provide WAL-tail exporter
primitives plus a pure Grok/Sentry-compatible mapper outside the hot path.

This patch does not wire the current runtime, send/cancel orders, call venue
REST or WebSockets, connect PostgreSQL, initialize Sentry, edit systemd, access
the VPS or start a canary. The existing `bbot.private.journal.v1` remains
unchanged and is not extended.

Read first:

- `AGENTS.md` in full;
- `docs/execution-v2-architecture.md`;
- `docs/execution-v2-canary-roadmap.md`, especially EV2-05;
- EV2-02 through EV2-04 task packets;
- `app/bot/execution/contracts.py`, `state_machine.py`, `adapters.py`;
- `app/bot/private/journal_v1.py` as historical evidence only;
- `app/bot/sentry_setup.py` and focused Sentry tests for compatible public
  names/fingerprints only.

## Pipeline block

```text
ExecutionEvent
      |
      v
non-blocking bounded critical queue -- enqueue ack is NOT durability
      |
      v
explicit writer worker: canonical JSONL -> flush -> fsync -> durable ack
      |
      +--> strict hash-chain replay -> historical SpreadState
      |                               + mandatory restart/reconciliation gate
      +--> idempotent projector cursor (memory implementation in EV2-05)
      +--> asynchronous exporter cursor -> pure Sentry/metrics envelopes
```

## Existing gap being closed

`bbot.private.journal.v1` cannot be the EV2 ledger: it does not represent
cumulative partial-fill quantities, its event vocabulary is v1-specific and
its synchronous append/fsync would be a latency foot-gun if placed on the
signal-to-send path. EV2-05 therefore adds a sibling schema and leaves all v1
callers and validators behaviorally unchanged.

## Allowed changes

- add `app/bot/execution/wal.py`;
- add `app/bot/execution/exporters.py`;
- amend `app/bot/execution/__init__.py` only to export the reviewed EV2-05 API;
- add `tests/test_execution_wal.py`;
- add `tests/test_execution_exporters.py`;
- amend this task packet only to append implementation/review evidence.

Everything else is frozen, including `contracts.py`, `state_machine.py`,
`adapters.py`, `transport.py`, `journal_v1.py`, `sentry_setup.py`, all existing
`app/bot/private/**`, runtime/strategy/collector code, deploy/systemd, secrets
and VPS paths.

If a safe invariant needs a new EV2 event type, adapter snapshot, real
PostgreSQL/Sentry dependency or hot-path fsync, stop and report the exact gap.
Do not widen the diff silently.

## Required public design

Use stdlib only. Public value objects are immutable and have redacted public
serialization/repr. Suggested reviewed surface (names may be narrowed, not
silently broadened):

```text
SCHEMA_VERSION = "bbot.execution.wal.v2"

WalRecord
WalAppendAck
WalDurableAck
WalHealth
ReplayResult
WalError
WalIntegrityError
ProjectionAck
InMemoryProjector
ExecutionWal

ExecutionWal(path, *, run_id, max_queue, reserved_tail, max_durable_lag,
             clock_ns, fsync_fn, crash_hook)
  enqueue(event) -> WalAppendAck
  drain_once() -> WalDurableAck | None
  drain_all() -> tuple[WalDurableAck, ...]
  replay() -> ReplayResult
  health() -> WalHealth
  mark_venue_reconciled(token) -> None

SentryEnvelope
MetricsSnapshot
WalExportCursor
InMemoryExporter
map_lifecycle_to_sentry_envelope(event) -> SentryEnvelope | None
```

The exact worker orchestration may remain injected/test-driven. Construction
must not create a thread, task, socket, network connection or Sentry client.
File creation/write occurs only on an explicit writer/open operation.

## WAL record and integrity contract

One global contiguous `wal_seq` is reserved per successfully enqueued event.
The canonical JSONL envelope contains exactly:

```text
schema_version = bbot.execution.wal.v2
wal_seq          positive global integer
run_id           durable lineage id, equal to event.run_id
prev_hash        previous record_hash, genesis = 64 zeroes
event            ExecutionEvent.to_public_dict()
event_content_hash
event_sequence_hash
record_hash      sha256 of canonical envelope excluding record_hash
```

Requirements:

- canonical UTF-8 JSON uses sorted keys and compact separators;
- each durable record is one complete line ending in `\n`;
- validate exact keys, schema, types, hashes and `run_id`;
- validate `wal_seq == previous + 1` and `prev_hash` continuity;
- reconstruct `ExecutionEvent` through `from_public_dict`, then re-check its
  content/sequence hashes;
- never store raw frames, venue order/execution ids, credentials, signatures,
  account ids or forbidden payload fields;
- preserve cumulative `quantity`, `open_order_count`, reconciliation and
  generation evidence exactly through round-trip;
- do not date-partition the active file or reset the chain at midnight.

The injected path must name `wal.jsonl` under an explicit `wal.v2` directory.
Never resolve through environment defaults. Reject known collector/v1 trees,
including `/data/live`, `/data/bars` and `/data/bbot/journal`. Local tests use
temporary directories only.

## Append, durability and backpressure

`enqueue()` performs validation plus bounded `put_nowait`-equivalent admission
only. It must perform no file open/write/flush/fsync, executor hop, Sentry work
or PostgreSQL work. Its ack explicitly states `durable=false`.

The writer operation runs outside the future socket loop and performs, in
order: encode complete record, append full line, flush, fsync, advance durable
watermark/hash, produce durable ack. Per-record fsync is the conservative EV2-05
default. A failed write/flush/fsync latches WAL unhealthy; it is never reported
as durable and is never silently skipped.

Queue rules for capacity `C` and reserved tail `R`:

- reject invalid `C/R` configuration;
- when depth is at least `C-R`, reject only a new OPEN
  `INTENT_ACCEPTED`; this prevents starting more work while reserving space for
  already-started lifecycle, close, recovery, reconciliation, pause and fault
  evidence;
- when depth reaches `C`, nack every event and latch hard-unhealthy;
- a nack never consumes `wal_seq` and is explicit/redacted;
- every `ExecutionEventType` is critical WAL evidence; none is sampled or
  dropped;
- writer death, queue-full latch, integrity failure, fsync failure or durable
  lag above the injected maximum blocks opens;
- projector/exporter failure or lag does not make the WAL unhealthy.

Health exposes queue depth, next/durable sequence, lag, writer/integrity latch
and `blocks_opens`. Metrics may be sampled; lifecycle events may not.

## Crash and corruption semantics

Expected torn final line differs deliberately from v1:

- if the file does not end in `\n`, ignore only that incomplete final byte
  suffix and replay the valid complete prefix with `torn_tail=true`;
- before any later append, truncate/isolate the torn suffix to the last valid
  newline so it can never be joined to a new record;
- a blank/bad JSON line, exact-key violation, bad hash, sequence gap, chain
  break, run mismatch or corruption before the final incomplete suffix is
  middle corruption and fails closed;
- never skip or auto-repair middle corruption;
- same `(run_id, wal_seq, record_hash)` is projector/exporter idempotency;
  same key with another hash is corruption.

Inject deterministic crash hooks at: before write; after write before flush;
after flush before fsync; torn write without newline; and after fsync before
durable ack. Tests do not kill real processes.

## Replay and mandatory restart gate

Replay validates the complete durable prefix and folds its events through the
frozen EV2-02 `apply_events`, starting from `initial_spread_state(run_id=...)`.
It returns reconstructed historical state, records/watermark, torn-tail status
and integrity information.

Replay must never itself authorize a live open:

- `ReplayResult.opens_allowed` is always false;
- `ReplayResult.requires_venue_reconciliation` is always true, including an
  empty first-boot WAL and a replayed proven OPEN or FLAT state;
- WAL health starts with post-process-start venue reconciliation incomplete;
- only an explicit `mark_venue_reconciled` using a token tied to a durable,
  current-process reconciliation record may clear that part of the open gate;
- fake restart `FAULT`/generation events are forbidden;
- the durable `run_id` is reused across process restart. A different `run_id`
  against a non-empty WAL fails closed.

The frozen FSM may reconstruct OPEN/FLAT; that is historical state, not fresh
venue authority. EV2-06 will compose the WAL restart gate with engine gates.

## Projector contract

Provide only a protocol-shaped boundary and `InMemoryProjector`. Do not import
database drivers, read a DSN, create SQL migrations or require Docker.

- idempotency key is `(run_id, wal_seq)` plus `record_hash` validation;
- first apply advances a contiguous watermark;
- re-applying the same record is a no-op ack;
- a gap or same key/different hash fails closed;
- projector lag/failure never alters WAL durability or its close/recovery
  capability;
- public projection contains only redacted WAL/event fields.

## Exporter and Grok/Sentry compatibility

`exporters.py` must not import `app.bot.sentry_setup`, `sentry_sdk`, logging
network handlers or database clients. It consumes only durable `WalRecord`
values by an explicit cursor. Sink invocation is outside enqueue/writer calls.
Sink failure leaves the cursor at the last successfully applied record so a
retry cannot lose lifecycle evidence.

The pure Sentry mapper preserves compatible existing Grok-facing semantics:

- accepted OPEN intent may map to event `open`, trade id = intent id, current
  `theta_k1` message/tag vocabulary and fingerprint
  `("theta_k1", intent_id, "open")`;
- a compatible proven close/flat lifecycle may map to `close` with the same
  fingerprint shape;
- rejects, ACKs, fills and reconciliation remain WAL-only unless an existing
  compatible Grok event is proven by fixtures;
- mapper output contains no SDK object and performs no capture or flush;
- exporter filtering is not WAL dropping: the cursor advances deterministically
  across mapped and unmapped records;
- metrics expose queue depth, durable lag, fsync count and cursor lag without
  copying raw event payloads.

## Tests and acceptance evidence

At minimum cover:

1. cumulative partial/final fill quantity survives exact WAL round-trip;
2. enqueue ack is non-durable; only successful fsync yields durable ack;
3. enqueue performs no filesystem/Sentry/projector/exporter work;
4. OPEN reserved-tail admission and absolute-full nack behavior;
5. no nack consumes `wal_seq`; every accepted lifecycle event drains in order;
6. write/flush/fsync failures latch unhealthy and never claim durability;
7. torn final line replays valid prefix and is removed before later append;
8. middle JSON corruption, exact-key violation, hash mismatch, chain break,
   sequence gap and run mismatch fail closed;
9. durable `INTENT_ACCEPTED`, `REQUEST_SENT`, ACK, partial/final fill,
   reconciliation and flatness boundaries replay deterministically;
10. crash hooks at every required writer boundary expose the correct prefix;
11. replayed OPEN, FLAT and empty WAL all keep live opens blocked;
12. only a durable current-process reconciliation token may clear the restart
    portion of health; unhealthy/lag gates still dominate;
13. projector apply/reapply is idempotent; gap/conflict fails closed;
14. exporter sink failure is retryable without cursor advance or WAL damage;
15. Sentry open/close envelopes preserve compatible fingerprints and perform
    no import/init/capture/flush;
16. import/construction performs no network, env lookup, thread/task or live
    action;
17. public errors/reprs/serialization redact forbidden input;
18. frozen modules and legacy journal/Sentry behavior remain unchanged.

Run:

```text
PYTHONPYCACHEPREFIX=./.pycache python3 -m py_compile \
  app/bot/execution/wal.py app/bot/execution/exporters.py \
  tests/test_execution_wal.py tests/test_execution_exporters.py

PYTHONPYCACHEPREFIX=./.pycache python3 -m unittest \
  tests.test_execution_wal \
  tests.test_execution_exporters \
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

Report exact counts and the eight blocks required by `AGENTS.md`.

## Stop conditions

Stop and report instead of widening scope if:

- safe quantity storage requires editing journal v1 or frozen contracts;
- cross-process venue-frame dedupe or adapter snapshot is demanded;
- fsync, Sentry, metrics or PostgreSQL must execute in enqueue/dispatch;
- restart safety appears to require a fake venue/fault event;
- replay cannot fail closed without changing the EV2-02 FSM;
- tests require PostgreSQL, a DSN, credentials, VPS or live venue access;
- the WAL would be placed in a v1 journal, collector/D tree or date partition;
- same-loop transport or current runtime wiring would need modification.

Leave implementation changes uncommitted for Codex review and an independent
Cursor critic. Do not push, access the VPS or start any canary.
