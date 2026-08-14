# Production-like storage soak: 2026-07-31

> **Host migration (2026-08-10):** current production collector host is `root@38.180.94.108`. The IP below is the historical host for this report.

## 1. Pipeline

VPS `root@38.244.198.42`:

`collector → live parquet → archive → 5-minute compaction → sent → backup1tb`

- Runtime command: `/root/venv/bin/python app/screaner_b_o.py`
- Experiment root: `/data/experiments/prod_soak_20260731_095200`
- Configured duration: 3,600 seconds
- Actual collector duration: 3,606.464 seconds
- Start: `2026-07-31T14:43:48.342974Z`
- Stop requested: `2026-07-31T15:43:49.052909Z`
- Collector end: `2026-07-31T15:43:54.806487Z`
- Experiment complete: `2026-07-31T15:46:26.364185Z`

## 2. Evidence

Remote source of truth:

- Summary: `/data/experiments/prod_soak_20260731_095200/summary.json`
- State: `/data/experiments/prod_soak_20260731_095200/state.json`
- Config: `/data/experiments/prod_soak_20260731_095200/experiment-config.json`
- Orchestrator: `/data/experiments/prod_soak_20260731_095200/logs/orchestrator.jsonl`
- Collector runtime: `/data/experiments/prod_soak_20260731_095200/logs/collector-runtime.log`
- Collector console: `/data/experiments/prod_soak_20260731_095200/logs/collector-console.log`
- Compactor: `/data/experiments/prod_soak_20260731_095200/logs/compactor.jsonl`
- Transfer: `/data/experiments/prod_soak_20260731_095200/logs/transfer.jsonl`
- Snapshots: `/data/experiments/prod_soak_20260731_095200/logs/snapshots.jsonl`
- Failed batches: `/data/experiments/prod_soak_20260731_095200/logs/collector-failed-batches.log`
- Remote destination: `backup1tb:prod-soak-20260731-095200`

The prior agent timeout did not interrupt the VPS run. The remote orchestrator reached
`state=complete`, wrote `summary.json`, and left no experiment processes running.

## 3. Result interpretation

Data-path result: pass.

- Accepted/published: `9,150,909 / 9,150,909` rows
- Rejected, quarantined, spooled: `0`
- Writer failures and backpressure events: `0`
- Manifest/archive/output rows: `9,150,909 / 9,150,909 / 9,150,909`
- Missing archives/outputs, checksum failures, noncomplete manifests, tmp/inprogress orphans: `0`
- Compaction: 13 complete windows; all `row_count_match=true`
- Transfer: 13/13 successful; retries, watchdog kills and reconciliation failures: `0`
- Final live, pending, spool and transfer backlog: `0`
- Remote verification: 13 files, 445,936,209 bytes

Operational-readiness result: fail.

## 4. Risks and blockers

### Blocker 1: compactor validates a stale lifecycle path

Transfer moves completed files from `compacted/spread_*.parquet` to
`compacted/sent/spread_*.parquet`. Later compactor runs validate the old path and emit
`FileNotFoundError`.

Observed impact:

- 78 `compaction_alert` records
- 12 compactor invocations with exit code 1
- `maintenance_failures=12`

The final accounting proves no loss in this run, but repeated false failures break
idempotency and can hide a real maintenance failure.

### Blocker 2: expected shutdown exits nonzero

The orchestrator requested shutdown after 3,600.710 seconds. Collector drained the queue
and logged:

- `publisher_shutdown_begin | queue_depth=0 | mount_dead=False`
- `shutdown_flush_done | published_rows=9150909`

An uncaught `asyncio.CancelledError` then produced collector exit code 1. No forced kill
occurred. Data shutdown was complete, but process shutdown semantics were not successful.

## 5. Verified performance

- Writer latency: p95 19.4 ms; max 382.5 ms
- Heartbeat: 120 samples; maximum gap 31 seconds
- Compaction: 130.177 seconds total; 12.585 seconds maximum per window
- Compaction size: 1,000,745,128 → 445,936,209 bytes; 55.44% reduction
- Transfer settings: rclone 1.74.4, SFTP concurrency 8, chunk 128k, watchdog floor 0.5 MiB/s
- Transfer throughput: 1.288 MiB/s copy phase; 0.624 MiB/s end-to-end
- Maximum observed pending backlog: 1 file / 38,931,838 bytes
- Disk free: 9.255 → 7.804 GiB
- Final archive: 30,912 files / 1,000,745,128 bytes
- Final sent and remote: 13 files / 445,936,209 bytes

## 6. Minimal fix plan

1. Make completed-manifest validation lifecycle-aware: validate the current `sent` path
   after transfer, or record the authoritative current artifact path in state.
2. Treat cancellation caused by the expected shutdown signal as a successful exit only
   after publisher drain and accounting complete successfully.
3. Add focused regression tests for:
   - compactor rerun after output moved to `sent`;
   - expected SIGINT with complete writer drain;
   - nonzero exit retained for actual drain, accounting, or persistence failure.

No runtime code was changed as part of this report.

## 7. Rerun acceptance criteria

Repeat the same 3,600-second VPS soak only after both blockers are fixed. Accept only if:

- Collector runs at least 3,600 seconds and exits 0 after the requested shutdown.
- `forced_kill=false`; shutdown logs contain both drain begin and successful flush/accounting.
- Accepted = published; rejected = quarantined = spooled-unrecovered = 0.
- Writer failures and backpressure drops = 0.
- Heartbeat maximum gap ≤ 60 seconds.
- Write latency p95 ≤ 50 ms and max ≤ 1,000 ms.
- Every eligible window has exactly one complete manifest and valid output.
- Compactor reruns after transfer produce no stale-path `FileNotFoundError`.
- `maintenance_failures=0`; no noncomplete manifests or compaction alerts.
- Manifest rows = archived source rows = output rows; all deltas = 0.
- Checksum failures, missing archives/outputs, corrupt/tmp/inprogress orphans = 0.
- Every compacted output reaches `sent` and remote with matching size/checksum.
- Transfer failures, reconciliation failures, retries exhausted and watchdog kills = 0.
- Final live, compacted-pending, spool and remote-transfer backlog = 0.
- No experiment processes remain, and `state.json` is `complete`.

## 8. Recommended next step

Fix blocker 1 first, then blocker 2, run scoped regression tests, and only then repeat the
same production-like soak. Current verdict: **NOT READY**.
