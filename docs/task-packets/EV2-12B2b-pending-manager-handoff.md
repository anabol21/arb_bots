# EV2-12B2b — durable pending EV2 → K=1 journal handoff

Status: local no-order handoff primitive and tests; **not runtime-wired**.
Live start remains blocked by `ev2_live_execution_adapter_not_integrated`.
This does not complete EV2-12B or authorize an order.

## Pipeline block and existing files

```text
fsynced EV2 WAL replay + exact state match (B1)
  -> pending candidate row in existing theta_trades journal
  -> strict restart replay latches K=1 until reconciliation
  -> [B2c/C: signed quantities + journal OPEN/CLOSE + slot publication]
```

`app/bot/execution/manager_candidate_journal.py` stages B1 evidence with
the WAL run/sequence/hash, two-leg projection, policy ID and a deterministic
candidate ID. It uses `ThetaTradeJournalWriter`, not a second exposure
ledger. `app/bot/theta_trade_manager.py` now recognizes the pending row on
replay and blocks normal K=1 decisions instead of treating the slot as safely
flat. The current collector, `would_sent`, strategy policy, live broker and
runtime startup gate are unchanged.

## Design choice, failure modes, and experiment

Directly publishing an OPEN/CLOSE from the WAL candidate was rejected:
venue positions can change after private gaps or process crashes. Ignoring
the candidate on restart was also rejected: an in-flight position could be
misread as an available K=1 slot. The selected row is explicitly
`pending_venue_reconciliation`, with `send=false`, `would_send=false` and
`lifecycle_committed=false`. It cannot itself become a trade lifecycle.

Under an isolated **single-writer** data root, retry after a crash scans
the same journal. An identical WAL sequence and evidence returns the same
candidate ID without appending; changed evidence for that sequence fails
closed. If append raises after a possible fsync, the result is ambiguous:
replay may find the row, but neither case authorizes a new order or slot
publication. An occupied manager slot rejects a new OPEN candidate.

The handoff is not yet safe for concurrent writers: ownership lease and
atomic producer/consumer sequencing are still EV2-12B integration work.
No resolution marker exists yet, so a staged candidate deliberately keeps
the manager blocked after restart. B2c must reconcile signed two-venue
quantity/order evidence, commit an idempotent OPEN/CLOSE row, resolve the
pending marker, and only then publish/release K=1. A loss of WAL/journal
correlation or failed venue proof is a stop condition, not a retry signal.

## Validation and success criteria

Code and tests run only in this local worktree; journal/WAL files are
materialized and fsynced under temporary local roots. No target-VPS
service, configuration, credentials or orders are changed. Tests cover
first staging, replay after process recreation, ambiguous append after
fsync, conflicting proof, occupied K=1, and the pending replay latch.
Before any VPS use, run the full crash matrix on an isolated no-order data
root, with explicit account ownership and venue reconciliation evidence;
verify collector and continuing `would_sent` remain healthy.

Next: EV2-12B2c/C venue-confirmed lifecycle commit and pending resolution,
then actual runtime wiring and fault injection. The live-start block stays.
