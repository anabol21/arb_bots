# EV2-12B2a — K=1 trade-history directory durability

Status: local durability fix and fault-injection tests only. This is a small
part of EV2-12B, not a live execution adapter or canary approval.

## Pipeline block and existing modules

```text
theta lifecycle row -> file write + fsync -> directory-entry fsync
  -> K=1 in-memory slot publication
```

`ThetaTradeJournalWriter` already fsynced the new file and the date and
`theta_trades` directories, while `ThetaTradeManager._commit_lifecycle_row`
published the slot only after append returned. But creation of the
`theta_trades` entry in `data_root` lacked a parent-directory fsync. A power
loss after the first row could therefore lose that directory entry despite
the file fsync. This patch adds `data_root` to the new-file fsync chain.

## Design, risks, and minimal experiment

The alternatives were a second manager ledger or a broader journal rewrite;
neither is needed for this boundary. The same append-only `theta_trades`
journal remains the K=1 recovery source. A fault injected at `data_root`
fsync must latch `trade_history_write_failed` and leave the in-memory slot
unchanged. On restart, the on-disk outcome of any failed fsync is **unknown**;
strict replay plus independent venue reconciliation, not a blind duplicate
append, must resolve it.

`data_root` itself is assumed to be a pre-created, durable release directory.
Creation of that release directory and its parent entry belongs to the
EV2-12D deployment preflight. This patch does not prove storage-controller
power-loss behavior, multi-process serialization, or atomicity between EV2
WAL and K=1 journal.

## Local and VPS validation / success criteria

The code is edited and tested in the local Git worktree. Test files are
materialized under a temporary local data root and replayed from
`theta_trades`; no VPS code, service, or storage is changed. Tests verify
all three directory fsyncs on first file creation and fail-closed K=1
behavior when the last fsync raises. The existing restart/journal suite must
remain green. Before deployment, test on an isolated target-VPS data root,
capture WAL and journal paths/logs, and verify the release directory itself
was durably provisioned.

Next: EV2-12B2b binds the durable EV2 candidate to the **same** K=1 journal
with stable idempotency and signed two-venue exposure evidence. The runtime
live-start block remains until that integration and the crash matrix pass.
