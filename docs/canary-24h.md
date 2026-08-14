# 24h production canary procedure

> **Host migration (2026-08-10):** current production collector host is `root@38.180.94.108`. The IP below is the historical host for this report.

## Purpose

Prove collector + compaction + backup accounting on production paths for 24h
with daily row accounting: manifest rows == local consolidated rows == remote
confirmed rows (when the transfer manifest stores row counts).

## Launch

```bash
mkdir -p /var/log/spread /data/live /data/spool /data/compacted
cd /root/spread_staging
/root/venv/bin/python validation/canary_24h.py --action launch \
  --live /data/live \
  --compacted /data/compacted \
  --spool /data/spool \
  --runtime-log /var/log/spread/runtime.log \
  --failed-log /var/log/spread/failed_batches.log \
  --remote backup1tb \
  --remote-path spread-compacted \
  --duration-hours 24
```

Enable maintenance timers/cron from `docs/prod-unit-snippets.md` so compaction
and transfer run every 5 minutes during the canary.

### Remote path (important)

Durable rclone prefix in production systemd is **`backup1tb:spread-compacted`**
(`BACKUP_RCLONE_PATH=spread-compacted` on `spread-backup-transfer.service`).

`backup1tb:spread-canary-24h` does **not** exist and must not be treated as the
durable target. Older canary-status / launch examples may still say
`spread-canary-24h`; for monitoring and accounting, always use
`spread-compacted`.

When running account manually against the live canary:

```bash
/root/venv/bin/python validation/canary_24h.py --action account \
  --remote backup1tb --remote-path spread-compacted
```

## Monitor

```bash
/root/venv/bin/python validation/canary_24h.py --action status
/root/venv/bin/python validation/ops_alerts.py --once
/root/venv/bin/python validation/canary_24h.py --action account \
  --remote backup1tb --remote-path spread-compacted
```

Evidence:

- `/data/experiments/canary_24h/canary-status.json`
- `/data/experiments/canary_24h/daily-accounting.json`
- `/data/experiments/canary_24h/logs/canary.jsonl`
- `/var/log/spread/runtime.log`
- `/var/log/spread/compactor.log`
- `/var/log/spread/backup-transfer.log`

### Remaining-wall-clock checklist (while canary is running)

Check every few hours (and again near T+24h):

1. **Process:** `collector_alive=true`, same pid (or documented restart),
   `seconds_remaining` decreasing toward 0.
2. **Writes:** fresh `published` / `job_accounted` lines in runtime log;
   `queue_depth` not stuck high; `failed_batches.log` empty or explained.
3. **Compaction:** recent `compaction_complete` with `row_count_match=true`
   (~5 min cadence via `spread-compactor.timer`).
4. **Transfer:** recent `transfer_success=true` to
   `backup1tb:spread-compacted`; `transfer_watchdog_kills=0`;
   backlog files usually 0–1 between timer ticks.
5. **Disk:** `df -h /` / `ops_alerts` `disk_free_gb` — headroom was ~5.2 GiB
   early in the run; do not auto-delete datasets; escalate if free space
   trends toward &lt;2–3 GiB.
6. **Alerts:** `ops_alerts.py --once` ends with `ops_alert_ok` or explained
   alerts only.
7. **Accounting:** local `row_delta_local == 0`. Remote row delta may be
   unavailable (`transfers` has no `row_count`; states are `sent`, not
   `confirmed`) — cross-check with `rclone size/lsl backup1tb:spread-compacted`
   and/or download SHA verify as in
   `docs/backup-validity-pre-canary-20260803.md`.

Do **not** enable `spread-collector.service` while the canary owns the process.
Do **not** kill the canary for routine checks.

## Pass criteria (T+24h)

Mark PASS / READY only after wall-clock completion evidence:

- Collector remained up for ~24h without forced kill (or documented safe
  restart with continuity).
- Expected shutdown (if stopped) exits 0 after drain.
- `ops_alerts.py` stays clean or alerts are explained/resolved.
- Local daily accounting: `manifest_rows == local_output_rows`
  (`row_delta_local == 0`), no missing local outputs.
- Remote durable prefix `backup1tb:spread-compacted` holds the compacted
  files that transfer marked `sent` (size match; prefer SHA/row spot-check).
- No stale-path `FileNotFoundError` / `compaction_alert` storms after `sent/`
  moves.
- Disk did not force silent loss or collector death.

## Status discipline

A canary that is only launched is **not** READY. Mark READY only after
wall-clock completion evidence and zero accounting deltas (local; remote via
`sent` + rclone verify until row_count lands in the transfer schema).

## Mid-canary status note (2026-08-03 ~14:29 UTC)

Checked over SSH on `root@38.244.198.42` (~55 minutes into 24h):

| Item | Value |
|---|---|
| Verdict | **YES — running healthy for mid-canary** (not READY) |
| Collector pid | `298870` alive (`app/screaner_b_o.py`) |
| Launched | `2026-08-03T13:34:36Z` |
| Expected end | `2026-08-04T13:34:36Z` (`expected_end_epoch` ≈ 1785850476) |
| Elapsed / remaining | ~55 min / ~23.1 h |
| Paths | live `/data/live`, spool `/data/spool`, compacted `/data/compacted` |
| Runtime log | `/var/log/spread/runtime.log` |
| Durable remote | **`backup1tb:spread-compacted`** (status file still says `spread-canary-24h`) |
| Timers | `spread-compactor.timer` + `spread-backup-transfer.timer` active |
| Collector systemd | installed, **not** enabled (canary owns process) |
| Local accounting | manifest/local rows **9,200,000**, `row_delta_local=0` |
| Transfer sqlite | `sent=10` files (9 canary windows + 1 old smoke); no `confirmed` state |
| Remote objects | 9 files / ~384 MiB under `spread-compacted` |
| Backlog | 1 file / ~37 MB (in-cadence) |
| Watchdog kills | 0 |
| Compaction alerts | 0 |
| Failed batches | 0 lines |
| ops_alerts | `ops_alert_ok` (`disk_free_gb≈5.21`) |
| Disk | `/` ≈ **5.2 GiB free** (82% used) — watch headroom; `/data/experiments` ≈ 16 GiB |

Next operator check (same host):

```bash
cd /root/spread_staging
/root/venv/bin/python validation/canary_24h.py --action status
/root/venv/bin/python validation/ops_alerts.py --once
/root/venv/bin/python validation/canary_24h.py --action account \
  --remote backup1tb --remote-path spread-compacted
df -h /
```

Final accounting window: after `2026-08-04T13:34:36Z`.

## Disk cleanup mid-canary (2026-08-03 ~14:45 UTC)

Operator-approved erase of **old experiment trees only** on `root@38.244.198.42`.
Active canary roots untouched (`/data/live`, `/data/compacted`, `/data/spool`,
`/var/log/spread/*`, `/data/experiments/canary_24h`; collector pid `298870`).

### Would `/` fill before first retention erase?

**Yes — without cleanup.** At ~T+1.1h:

| Metric | Value |
|---|---|
| Free before | **4.8 GiB** (84% used) |
| Active pipeline footprint | live+archived ~1.4G, compacted+sent ~0.55G |
| Archive growth (first hour) | ~**1.3 GiB/h** (all archived mtimes within canary window) |
| Sent growth | ~**0.53 GiB/h** (~44 MB / 5-min window) |
| Sent retention | 12h → steady-state peak ~**6.4 GiB** |
| Archive retention | 24h → first archive deletes near canary end |

With only 4.8 GiB free, archive+sent growth would exhaust `/` well before
12h sent retention and long before 24h archive retention.

### Deleted (sizes at delete time)

| Path | Size |
|---|---|
| `/data/experiments/prod_soak_20260730_161700` | 12G |
| `/data/experiments/prod_soak_20260731_095200` | 1.5G |
| `/data/experiments/exp_20260728T2232Z` | 996M |
| `/data/experiments/prod_soak_20260803_122023` | 899M |
| `/data/experiments/throughput_20260729` | 406M |
| `/data/experiments/throughput_20260730_tuned` | 161M |
| `/data/experiments/backup_smoke_20260730` | 52K |
| `/data/experiments/backup_outage_check_20260803` | 12K |

Kept: `/data/experiments/canary_24h` (~20M).

### After delete

| Metric | Value |
|---|---|
| Free after | **~20 GiB** (30% used; was 4.8 GiB / 84%) |
| `/data/experiments` | only `canary_24h` |
| Collector | pid `298870` still alive; heartbeats continuing |

### Remaining disk risk (rest of 24h)

Cleanup removes the immediate fill risk from orphaned soaks. **WATCH** archive
growth (~1.3 GiB/h observed): over ~23h remaining that can approach ~30 GiB
before first archive retention, vs ~20 GiB free now. Sent caps earlier (~6.4 GiB
at 12h). Re-check `df -h /` every few hours; escalate if free &lt; 3 GiB.

## Early stop (2026-08-04 ~11:40 UTC) — FINAL for this canary

Operator-requested early stop (~1.9h before planned `2026-08-04T13:34:36Z`).
Full write-up: [`docs/canary-24h-early-stop-20260804-result.md`](canary-24h-early-stop-20260804-result.md).

| Item | Value |
|---|---|
| Verdict | **PASS WITH CONDITIONS** — unconditional **READY still blocked** |
| State | `early_stopped` in `/data/experiments/canary_24h/canary-status.json` |
| Shutdown | **SIGINT** to pid `298870`; gone in ~7s; `shutdown_flush_done` (no SIGTERM/KILL) |
| Exit code | not captured (session leader reaped by init); flush path clean |
| Elapsed / planned | **~22.1h / 24h** |
| Published | 119,543,737 rows / 402,192 files |
| Manifests | 264 complete; manifest rows 119,047,046 |
| Remote at stop | **263** files / **5.615 GiB** under `backup1tb:spread-compacted` |
| Remote shortly after | **264** files (stop-time backlog transferred) |
| Local missing after retention | 118 — **all on remote** (0 truly missing) |
| Sample SHA/rows | 3/3 pass (incl. early remote-only file) |
| Watchdog kills | 0 |
| Failed batches | 0 |
| Compaction alerts | **6873** stale-path `FileNotFoundError` after `sent/` retention |
| Disk at stop | `/` ≈ **5.6 GiB free** (81%) |
| Timers left running | compactor + backup-transfer active; collector unit still disabled |
| Backlog | stop-time file transferred; newer post-stop window may still drain |

READY blockers: alert storm after retention, local-only accounting blind to remote,
incomplete 24h wall-clock, transfer schema still `sent`-only, `ops_alerts` parse bug.
Next: fix retention/lifecycle alerting + accounting, then **lean soak** before another
READY claim. Do **not** enable `spread-collector.service` from this result alone.
