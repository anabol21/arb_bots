# Compacted bars: production bridge — 2026-08-13

## Граница и layout

- Source collector (не менялся): `/data/bars/bar_5m/base_coin=<COIN>/event_date=<UTC-date>/batch_*.parquet`.
- New compacted root: `/data/bars_compacted/bar_5m/base_coin=<COIN>/event_date=<UTC-date>/bar_5m_<start>_<end>_inputset=<16-hex>.parquet`; the input-set digest identifies the exact frozen source path/bytes/rows/SHA snapshot, not the hour alone.
- New remote prefix: `backup1tb:spread-bars-compacted`.
- Durable boundary: transfer manifest state `sent` только после remote
  temporary→final, final size check и SHA download verification.

Compactor пишет один closed coin×UTC-hour output за invocation, zstd,
`.inprogress`→fsync/read-back→atomic final, manifest с rows/input SHA/output
SHA. Source сначала перемещается в `/data/bars_archive/bar_5m`; retention
архива допускается только после `sent`. Legacy `/data/bars` и
`backup1tb:spread-bars` не удаляются и не drain-ятся этим rollout.

## Local evidence

`python3 -m pytest tests/test_bars_compactor.py tests/test_backup_transfer.py -q`
— **28 passed**. Покрыты happy path, rows/schema/readability, restart,
изменённый/corrupt source и запрет retention до remote confirmation.

## VPS early smoke

Host: `root@38.180.94.108`; collector and tick compactor remained active.
Legacy `spread-bars-backup-transfer.timer` remained `disabled`.

Isolated copied source batch produced:

```text
backup1tb:spread-bars-compacted/smoke2/base_coin=0G/event_date=2026-08-12/
bar_5m_20260812T130000Z_20260812T140000Z.parquet
```

It was readable as exactly the five bar body columns, contained one row and was
3,555 B locally/remotely. Transfer completed in 31.082 s including remote
temporary/final checks and SHA download verification; watchdog kills were zero,
and transfer backlog was zero.

Initial real cohort evidence after enabling the new timers: five readable
compacted outputs / 46 rows; three outputs reached transfer state `sent`;
`spread-collector.service` had `NRestarts=0`; `/data` remained 21% used
(about 61 GiB free). This is an early observation, not a throughput proof.

## Rollout correction

The first implementation discovered the full two-day hive every invocation and
cost about 31 CPU seconds per output. Timers were stopped before a long proof.
Discovery was narrowed to one coin/day partition per invocation, then measured
below one second for a real invocation; timers were re-enabled. This prevents a
legacy mass drain but still needs a measured 60–90 minute rate comparison.

## Timer scheduling correction

The first timer design used `OnBootSec` together with `OnUnitInactiveSec`.
After the timers were manually restarted at 07:53 UTC, their target
`Type=oneshot` services had already reached `inactive`: systemd 249 recorded
`TimersMonotonic ... next_elapse=0` and
`NextElapseUSecMonotonic=infinity`. `OnBootSec` had also elapsed long before
the restart. Therefore no future trigger was armed; this was a scheduling
failure, not a compactor or transfer failure.

The deployed timer definitions now use wall-clock schedules with
`Persistent=true`: `*-*-* *:*:0/10` for the compactor and
`*-*-* *:*:00` for the compacted-bars transfer. This removes the dependence on
an inactive transition of a pre-existing oneshot service and makes missed
calendar occurrences observable/recoverable. The timer gate is two consecutive
actual triggers for each timer, with non-infinite `NextElapse`, before starting
the focused proof.

## Focused proof: early stop — 2026-08-13

The calendar timers recurred: the compactor fired every ten seconds and the
backup service started at 19:16, 19:17, and 19:18 UTC. `NextElapse` is visible
while a timer is waiting; for the one-minute backup timer it is temporarily
`infinity` while the associated oneshot is running, then the next calendar
occurrence starts after the service completes.

The proof began at `2026-08-13T19:24:37Z` and was stopped at 20 minutes rather
than extended to 90: the transfer manifest recorded two repeatedly failed
objects. For the current 19:00–20:00 UTC hour, local outputs no longer matched
the existing remote final objects:

- `0G`: local 3,579 B versus remote 3,555 B;
- `1INCH`: local 3,571 B versus remote 4,620 B.

Each reached 15 attempts, and the one-minute transfer service emitted 28
`status=1/FAILURE` / failed-result events. The transfer correctly refused to
overwrite a mismatched remote final, so the affected outputs remain local and
the source archive is retained; this is not accepted as durable completion.
The evidence indicates that a compacted output path was rematerialized after a
remote object had already been confirmed, which violates the immutable
output-to-remote-object lifecycle required by this bridge.

At the stop point the collector remained active with `NRestarts=0`; the legacy
tiny-bars timer was disabled; `/data` free space remained about 57.6 GiB; and
all 153 sampled compacted files (1,674 rows) were readable. However, the
durability failure is decisive. Both new compacted-bars timers were stopped to
contain repeated failed transfer attempts. Do not start the overnight recanary
until the path-rematerialization lifecycle defect is fixed and a new focused
proof passes, including fresh confirmed remote samples for BTC, ETH, and SOL.

## Critical lifecycle defect: read-only VPS trace

The failure is reproducible from the preserved artifacts; no local or remote
object was deleted, moved, or overwritten during this investigation.

- Both compacted timers and the legacy tiny-bars timer are `disabled`; their
  services are `inactive`/previously `failed`. `spread-collector.service` is
  `active`.
- Current active local finals for `19:00–20:00Z` are `0G` 3,579 B /
  `2c9bc76f…81337f9` and `1INCH` 3,571 B /
  `4efd4b24…622b98`.
- The retained `sent/` copies and remote finals remain different, but match
  each other exactly: `0G` 3,555 B /
  `82c99b48…1e4e2d8`; `1INCH` 4,620 B /
  `dd24cebe…9afab72`.
- The transfer rows show 15 attempts for each identity and
  `remote final size mismatch`; their old `sent_at` values prove a previous
  object with the same hour-only name was already confirmed before the new
  local file appeared.
- The `0G` v1 sidecar is again `planned` for a new 3,555 B source while the
  local final is 3,579 B. The compactor accepted that existing final on row
  count alone, rewrote the manifest's output checksum, and retained the
  incompatible source/output relationship. `1INCH` shows the same
  hour-only-identity collision with a different former input set.

### Root cause

This is not an rclone corruption or timer recurrence failure. Three compactor
defects combined:

1. Discovery used only `source.mtime <= now - grace`; it could compact a
   partial **current open hour** before `window_end`.
2. A later source batch for that same hour was eligible again because discovery
   did not treat a terminal manifest as the frozen owner of the window.
3. Output and sidecar names used only `(coin, date, hour)`. New discovery
   overwrote the old sidecar. On restart/rematerialization an existing final
   was accepted by rows/schema only, without comparison to a frozen expected
   SHA. The backup layer correctly refused a size/checksum conflict but
   continued retrying the same damaged identity.

## Selected repair (layout v2)

- Eligibility is `now >= window_end + grace` and source mtime no newer than
  that boundary. A current or mutable hour is excluded.
- The first v2 manifest freezes sorted source path, bytes, rows and SHA-256.
  Output identity has a deterministic input-set digest suffix:
  `bar_5m_<start>_<end>_inputset=<16-hex>.parquet`.
- A later source for a frozen hour is left untouched in the source root and
  gets a durable quarantine record/alert. It is not silently appended and does
  not create a second model-visible file. A generation with new semantics
  requires an explicit migration decision, not timer retry.
- Materialization always builds to a unique temporary file. Existing finals
  are accepted only when the rebuilt output SHA is identical; otherwise the
  process errors without overwrite. Archive move rechecks each source SHA.
- Transfer marks a remote-final size/content collision as terminal `conflict`
  and excludes it from subsequent candidate runs. Exact remote content remains
  idempotently confirmable. Existing v1 `planned`/`published` manifests are
  quarantined in place rather than resumed.

Local test gate after the repair:

```text
python3 -m pytest tests/test_bars_compactor.py tests/test_backup_transfer.py -q
33 passed
python3 -m py_compile app/storage/bars_compactor.py app/storage/backup_transfer.py app/schema/parquet_layout.py
```

Coverage includes restart recovery, existing remote same/different content,
terminal conflict non-retry, open-hour exclusion, late source quarantine and
input-set collision-safe naming. Local success does not establish VPS or remote
durability.

### VPS focused lifecycle smoke after deployment

The repaired modules were copied to `/root/spread_staging` and passed
`/root/venv/bin/python -m py_compile`. With all recurring bars timers still
disabled, an isolated copied **closed** `0G` cohort (`18:00–19:00Z`) was run
under `/tmp/spread-bars-v2-smoke-20260813`; production source/archive and the
two collided `19:00–20:00Z` artifacts were not changed.

- Compactor wrote one 2-row / 3,579 B v2 output named
  `bar_5m_20260813T180000Z_20260813T190000Z_inputset=a1d8ac78658a797f.parquet`.
- Local parquet readability/schema smoke passed (`errors=[]`).
- A single transfer to the separate new remote smoke prefix used
  temporary→final, final-size verification and SHA download verification in
  29.65 s; `backlog_files_count=0`, `transfer_success=true`, watchdog kills 0.
- Retained local sent object and remote object both have 3,579 B and exact
  SHA-256
  `198558113270fde06e38e43d57f687c44942f3b6bbc895f82258993d71ab20b5`.

This is a clean focused lifecycle smoke, not a throughput proof and not a
license to re-enable timers.

## Current verdict

**NO-GO: compacted-bars canary remains stopped.** The current mismatch
artifacts are intentionally untouched/quarantined. Required remaining gates:

1. Deploy the v2 patch while timers stay disabled; first verify v1 artifacts
   become quarantine records without a source/archive move.
2. Run a manually selected **new already-closed** cohort through one
   compactor/transfer smoke and compare local/remote bytes, SHA and parquet
   readability.
3. Independently review the patch and repeat the VPS lifecycle scenario.
4. Only then consider re-enabling timers for a 60–90 min proof. The 90-minute
   proof and overnight recanary remain forbidden until the focused smoke is
   clean.

## v2 review-finding deployment and one manual cohort — 2026-08-13

The reviewed v2 safeguards were deployed to `/root/spread_staging`; deployed
SHA-256 values matched the local files. Before the cohort, the last v1
`planned` manifest was converted in place to terminal `quarantined`; there
were no remaining v1 `planned` or `published` manifests. No legacy final,
remote collision artifact, or source was overwritten or deleted.

With both recurring compacted-bars timers still `disabled`/`inactive`, the
manual closed production cohort was:

```text
base_coin=2Z/event_date=2026-08-11/
bar_5m_20260811T120000Z_20260811T130000Z_inputset=168b364b121fa740.parquet
```

Its v2 identity was confirmed absent locally and remotely before the run. The
compactor froze two source files / three rows, wrote a 4,588 B final with
SHA-256 `88a4b170fcf6515499d9b1ea6a0eee7893e402f935becbb0d5d9354169937f39`,
then archived the sources only after local publication. One manual backup
attempt reached `sent`; the retained sent copy and a timeout-bounded streamed
remote SHA-256 both exactly matched the manifest SHA. This proves one lifecycle
only; recurring-proof timers remain stopped pending independent review and the
separate 60–90 minute gate.

The read-only full-root validator also correctly reported the retained legacy
hour-only v1 filenames as non-v2 identities and two historical failed transfer
rows (7,150 B pending). Those preserved artifacts are not part of the clean v2
cohort and were not modified. Consequently, the focused v2 lifecycle passed,
but this evidence alone is not a GO to enable the recurring-proof timers.

## v2 root isolation — deployed, recurrence held

The historic validator result is valid for the mixed v1 root but is not a
v2-path failure. The production v2 route is explicitly isolated:

- output/state/sent: `/data/bars_compacted_v2/bar_5m`;
- archive: `/data/bars_compacted_v2/archive/bar_5m`;
- remote: `backup1tb:spread-bars-compacted-v2`;
- unchanged source: `/data/bars/bar_5m`.

The former `/data/bars_compacted`, `/data/bars_archive`, and
`backup1tb:spread-bars-compacted` remain preserved read-only legacy artifacts.
There is no automatic migration, deletion, overwrite, or bulk move. The v2
validator default and both v2 systemd units point only to the isolated roots,
so historic v1 hour-only files and failed transfer rows cannot contaminate
v2 recurrence evidence.

### Isolated v2 smoke and recurrence hold — 2026-08-13

The isolated roots and units were deployed to `root@38.180.94.108` with both
v2 timers still `disabled`/`inactive`. Local and deployed SHA-256 values for
the schema, compactor, transfer, validator, and both units matched. The new
local root and remote prefix were absent before the smoke.

One copied, already closed `0G` `19:00–20:00Z` source cohort was compacted into
the new production v2 root:

```text
base_coin=0G/event_date=2026-08-13/
bar_5m_20260813T190000Z_20260813T200000Z_inputset=3dc0a630a20b508c.parquet
```

It contained six frozen source files / eight rows and produced an 8,648 B
output. The first transfer reached `sent` in 29.62 s with zero retries,
zero watchdog kills, and zero backlog. The retained local sent copy and
streamed remote SHA check passed; the v2 validator reported one readable file,
zero errors, zero pending rows, zero conflicts, and zero quarantine records.
The validator now also accepts the legal interval where transfer has moved a
file to `sent/` but the next compactor run has not yet advanced its manifest
from `archived` to `remote_retained`.

This is not a recurrence GO. The smoke intentionally copied rather than moved
the six original source batches. A read-only candidate check against the live
source now returns those same six batches as
`reappeared_source_for_frozen_window` for the frozen v2 manifest. Enabling the
timer would create unexpected quarantine records rather than demonstrate a
clean drain. Timers remain stopped; the 60–90 minute proof and 8–12 hour
re-canary were not started. No v1 or v2 artifact was deleted, overwritten, or
bulk-moved. At the hold point the collector was `active` with `NRestarts=0`,
there were no OOM/TERM journal signals since 19:00 UTC, and `/data` had 58 GiB
free (25% used).
