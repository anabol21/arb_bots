# EV2-12B1 — fsynced WAL fence for manager lifecycle candidates

Status: local pure/read-only contract and tests. EV2-12B is **not complete**;
live order start remains blocked by `ev2_live_execution_adapter_not_integrated`.
No VPS service, credential, strategy policy, collector, or `would_sent` change.

## Pipeline block

```text
EV2 state / accepted WAL queue
  -> WAL drain + fsync
  -> strict replay of the durable prefix + WAL health
  -> exact replayed-state match
  -> proposed OPEN/CLOSE evidence tied to WAL run/sequence/hash
  -> [B2: venue reconciliation + durable K=1 journal + publication]
```

## Existing files/modules and design choice

- `app/bot/execution/wal.py` accepts events into a queue before `drain_once`
  fsyncs them. `ExecutionEngine._commit_events` advances in-memory state on
  queue acceptance; therefore that state alone is not durable proof.
- `app/bot/execution/manager_projection.py` only proposes OPEN after proven
  two-leg exposure or CLOSE after proven flatness; it performs no I/O.
- `app/bot/execution/durable_projection.py` now rejects a candidate while the
  WAL has pending events, lag, a torn tail, failed writer/integrity, mismatched
  watermark, or a replayed state different from the engine state. A surviving
  candidate carries the last durable record's run ID, sequence and hash.
- `tests/test_execution_durable_projection.py` exercises queue versus fsync,
  restart replay, ACK-only, state mismatch, unhealthy WAL, and durable CLOSE.

Do not infer a lifecycle from a queue ACK or write a second independent
position ledger. A pure candidate is deliberately weaker than publication:
the B2 consumer must take replay and health from the **same** WAL under a
consistent snapshot, reconcile signed venue state, fsync the K=1 lifecycle
row, and only then update the visible manager slot. A crash after an exchange
write but before WAL fsync remains ambiguous and must be resolved from stable
client IDs and venue evidence; never blindly retry.

## Risks and next experiment

This patch does not integrate the live sender, private event ingestion,
ownership lease, signed REST reconciliation, or manager journal. It cannot
establish exactly-once execution or release K=1. The next B2 patch should
bind these components around one intent and inject crashes before write,
between legs, after ACK, after one fill, before WAL fsync, and before manager
journal fsync. Unknown or unilateral exposure blocks new opens.

## Validation / success criteria

Local edit, execution and test logs: this worktree and temporary test files.
The WAL tests materialize and fsync local temporary files; there is no target
VPS run or production durability claim. Pass condition: accepted-but-not-
fsynced OPEN/CLOSE produces no candidate; exact fsynced replay yields the
same candidate after process recreation; ACK-only and unhealthy WAL produce
none. Before any target-VPS deployment, repeat the fault matrix in an
isolated no-order data root and verify collector and `would_sent` remain
unchanged. No live capability is unlocked by this packet.
