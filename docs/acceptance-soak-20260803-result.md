# Acceptance soak result: 2026-08-03

> **Host migration (2026-08-10):** current production collector host is `root@38.180.94.108`. The IP below is the historical host for this report.

## 1. Pipeline

VPS `root@38.244.198.42`:

`collector → live parquet → archive → 5-minute compaction → sent → backup1tb`

- Runtime command: `/root/venv/bin/python app/screaner_b_o.py`
- Code root: `/root/spread_staging`
- Experiment root: `/data/experiments/prod_soak_20260803_122023`
- Remote: `backup1tb:prod-soak-20260803_122023`
- Configured duration: 3,600 seconds
- Actual collector duration: 3,606.663 seconds
- Knobs: 337 pairs, `PERSIST_EVERY_N=100000`, publisher queue 4, subscribe 30/3s
- rclone: `/opt/rclone-1.74.4/rclone`, concurrency 8, chunk 128k

## 2. Patches under test

- `app/screaner_b_o.py`: expected cancel does not re-raise after successful drain; nonzero retained on primary-storage failure
- `app/storage/compactor.py`: lifecycle-aware validation for `compacted/` and `compacted/sent/`; `artifact_location` on manifests

## 3. Hard criteria verdict: PASS

| Criterion | Result |
|---|---|
| Collector exit code 0 | **pass** (`returncode=0`) |
| `forced_kill=false` | **pass** |
| `maintenance_failures=0` | **pass** |
| No stale-path `FileNotFoundError` | **pass** (0) |
| `compaction_alert` count | **pass** (0) |
| Final backlog files/bytes | **pass** (0 / 0) |
| Watchdog kills | **pass** (0) |
| Accounting deltas | **pass** (manifest/archive/output rows all 5,458,497; deltas 0) |
| Transfers | **pass** (13/13 `sent`) |

## 4. Key metrics

- Accepted/published rows: 5,458,497
- Complete manifests: 13; noncomplete: 0
- Missing archives/outputs/checksum failures: none
- Remote verification: 13 files / 280,166,201 bytes
- Writer latency (heartbeat samples): p95 15.591 ms; max 29.446 ms
- Heartbeat: 120 samples; max gap 31 s
- Shutdown logs:
  - `main | cancellation received, shutting down tasks`
  - `publisher_shutdown_begin | queue_depth=1 | mount_dead=False`
  - `shutdown_flush_done | published_rows=5458497`

## 5. Evidence paths

- Summary: `/data/experiments/prod_soak_20260803_122023/summary.json`
- State: `/data/experiments/prod_soak_20260803_122023/state.json`
- Orchestrator: `/data/experiments/prod_soak_20260803_122023/logs/orchestrator.jsonl`
- Collector runtime: `/data/experiments/prod_soak_20260803_122023/logs/collector-runtime.log`
- Compactor: `/data/experiments/prod_soak_20260803_122023/logs/compactor.jsonl`
- Transfer: `/data/experiments/prod_soak_20260803_122023/logs/transfer.jsonl`

## 6. Soak readiness

**READY WITH CONDITIONS**

Conditions (residual / operational, not soak blockers):

1. Install/enable systemd units or cron from `deploy/` only after operator review.
2. Complete 24h canary wall-clock evidence on production paths (`docs/canary-24h.md`).
3. Keep rclone at concurrency=8 / chunk=128k (do not return to 32/512k).

## 7. Prior blockers closed

1. Compactor stale-path false failures after `sent/` moves — not observed (0 alerts / 0 FNF).
2. Expected shutdown nonzero exit — closed (`returncode=0` after drain).
