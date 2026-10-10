# Gear 2.3 live canary launch report — 2026-10-06

## Deployment result

The latest B2.3 startup attempts ended before the signal loop, so no canary
process is currently running. The last successful rollback-health run was PID
`2360461` from immutable commit `e2e134ca54731af95f1a8ea9594372e872fda025`;
that PID was gracefully stopped before deploying the ACK-handoff fix. The
current checkpoint retains `source_pid=2360461`, but that PID is no longer
running. The checkpoint is flat with `pending=false`, no halt, and deadline
`2026-10-09T12:27:03.428000Z`. The B2.3 branch includes the validated preB2.2
contour, Gear 2.3 patches A/B, and the reviewed close-PnL correction. `main` is
`1d43137d208d4921ddafb406e6d267c861b3d318`; it retains the separate collector
and would-send production contour.

The current process is a standalone terminal-private canary. The separate
would-send process (PID `1938088`) remains the source of the cumulative pool
snapshot and is unchanged. The prior Gear 2.2 terminal-private process (PID
`2224799`) stopped cleanly before this launch. The downtime from its final
checkpoint at 12:08:08 UTC to private warm readiness at 12:27:21 UTC was about
19 minutes 13 seconds.

## Launch and recovery chronology

The old canary's final checkpoint was flat: `position={}`, `pending=false`, no
halt, and its process had exited. Its journal contained one closed intent and
no unresolved intent. The prior resume manifest was deliberately not reused;
the B2.3 launch started a fresh 72-hour window after the flat startup check.

Two launch setup failures were contained before signal processing. The first
restricted package omitted the dynamic `floors.py` dependency. After adding
that Python source and the required policy modules, constructor, floor observer,
warm-pickle, import, and syntax smoke checks passed. The next runtime attempt
exposed a sequencing defect: OKX trade authentication timed out while the
multi-coin private REST reseed was running. No orders were sent in that failed
attempt. The fix authenticates both private and trade sockets first, enables
owner-loop receive/heartbeat work, then runs the existing matched REST reseeds.
Send readiness still requires successful reseeds. The affected warm-session
tests passed 7/7; no order, fill, leverage, or settings path changed.

The first start of PID `2343195` passed the private warm and 29-coin flat-state
checks. A launcher variable typo made hot-add look for the default
`data/hot_add_delta.csv` while the cumulative source remained at
`/data/bbot-would-send-prod/hot_add_delta.csv`. After verifying the default
path was absent and the production file had 25 unique rows, a symlink was
created at the default path pointing to the production snapshot. The production
CSV was not modified. The existing 30-second poller then reported `rows=25`,
`added=25`, and `extra=25`. Future launch commands must use the actual variable
`BBOT_HOT_ADD_DELTA` if configuring an explicit path.

## Runtime configuration and state

The source tree runs on the VPS at
`/root/b-private-b-exp/response-manager-code/response-handler-20261006-gear23-B2.3-e2e134c/gear23/`.
Logs are written to
`/root/b-private-b-exp/response-manager/20261005T194200Z-gear22-longrun/bbot-gear23-b2.3.log`.
Runtime and private journal files are materialized under the existing persistent
roots:

- `/root/b-private-b-exp/response-manager/20261005T194200Z-gear22-longrun/data/`
- `/root/b-private-b-exp/response-manager/20261005T194200Z-gear22-longrun/private/`

The checkpoint reviewed for position/pending state is
`/root/b-private-b-exp/response-manager/20261005T194200Z-gear22-longrun/data/canary_state.json`.
The old `spread-bbot-gear22-live-canary` systemd unit is expected to remain
inactive. PID `2343195` is a standalone process, not managed by that unit, and
no reboot autostart is configured. Monitor the PID and `/proc/2343195/cwd`,
this log, and heartbeat/state evidence; systemd status does not report this
canary's health.

The hot-add path in the data root is a symlink to
`/data/bbot-would-send-prod/hot_add_delta.csv`. The bot reads the production
snapshot; the would-send process owns and updates it. History is read from
`/data/bbot-would-send-prod-history`. Files are materialized on the VPS data
volume; no remote-upload or remote-durability claim was tested.

The first B2.3 process started at `2026-10-06T12:27:02.937Z`. After the
flat-only recovery restart, the preserved open-window deadline is
`2026-10-09T12:27:03.428000Z` (15:27:03 MSK); this is the deadline retained
through the failed startup attempts. The last successful process checkpoint reported `position={}`, `pending=false`,
`halt=null`, and `completed_cycles=0`; it is not a live health check after
process exit. The configured limits remain K=1, notional 10 USDT,
policy `gear22_frozen_v1`, `canary_max_cycles=0`, and 72 hours. It uses the
existing natural signal and halt rules; no synthetic signal or forced order was
issued. The initial process started with the original 29 coins carrying
existing confirmed-1x evidence. The later user-authorized 1x preparation is
recorded below; no other account or strategy settings were changed.

## Readiness evidence at initial review

At startup, `private_warm_started` reported `ready=True`,
`handshake_count=1`; the startup flat check confirmed 29 coins. Heartbeats from
both Bybit and OKX trade sockets continued after warm readiness. After the
snapshot was linked, all 25 extras were registered. Public ACKs were observed
for all 54 coins on both venues: 54 Bybit `orderbook.1` and 54 OKX `books5`
subscriptions. Observer warm events covered 25 extra coins. Candidate gate
events initially showed all 25 extras blocked for missing prep-confirmed 1x;
per the runtime gate ordering, an extra reaching this reason has passed its
instrument-scoped private readiness checks at that evaluation. Public
`stale`/`incomplete` reasons were also present while books were settling. No
per-coin raw private ACK count is claimed because those ACKs are tracked in
memory rather than journaled as an independent per-coin summary.

No order or leverage mutation was attempted at this initial review. The live
order path remains enabled only for natural policy decisions under the existing
guards; no qualifying signal had been observed at this initial review. The
standalone would-send process is separate and continues unchanged.

## Monitor and stop conditions

Read the single PID, log, and state above for runtime health. Confirm state is
flat and `pending=false` before any operator restart. A held position or
pending terminal action must resolve through the existing natural close path;
do not force-close or kill the process. Extra coins without matching 1x
evidence or another required readiness gate remain blocked. Do not change
profile, K, notional, selector, or halt settings as part of this canary.

For rollback, first require the checkpoint to show `position={}` and
`pending=false`, then stop the currently recorded canary PID gracefully before
deploying the prior source. If a position is held or an action is pending,
preserve the current source and journal and use only a compatible handoff after the existing
natural close completes. Never force-kill or auto-flatten during rollback.

## 1x preparation and restarted run

After explicit user authorization, the exact 25 extras in the cumulative
production snapshot were prepared through the existing fixed-1x helpers. The
snapshot was `/data/bbot-would-send-prod/hot_add_delta.csv`, SHA256
`29d5f0024c25c120db3ec3edbf6315ddc4c7ceb1376b1b12be1dfd0dd11c80d5`. Every
target passed both-venue symbol-scoped flat-position and open-order checks
before settings calls. The runner made 50 fixed leverage-to-1 setting posts;
25/25 Bybit and 25/25 OKX setter acknowledgements and matching 1x readbacks
were confirmed. It sent zero orders. Non-secret evidence is at
`/root/b-private-b-exp/response-manager/gear23-1x-prep-20261006-502ab09-r2/result.json`.
The resulting confirmation records the 29 base coins unchanged and 54 total
confirmed coins. No account-mode reads or unrelated settings changes were
made.

With the checkpoint flat, `pending=false`, and no halt, PID `2343195` received
SIGTERM and exited. Its final checkpoint remained flat with no pending action.
The same immutable runtime commit `e2e134ca54731af95f1a8ea9594372e872fda025`
restarted as PID `2354286` at `2026-10-06T13:24:52.968Z`. It reuses the existing
data, private-journal, and log roots and reads the production CSV directly via
`BBOT_HOT_ADD_DELTA=/data/bbot-would-send-prod/hot_add_delta.csv`. The 29 base
coins remain unchanged; the confirmed-1x list now contains 54 coins. The
original open-window deadline is preserved as
`2026-10-09T12:27:03.176Z`, about 0.239 seconds later than the previous
checkpoint deadline due to floating-point hour conversion.

The new process reported `private_warm_started ready=True` with one handshake,
confirmed startup flat state for 54 coins, and emitted heartbeats. Its hot-add
poll read 25 rows and retained 25 extras. The startup registration passed all
54 coins to the private warm-session pool. A filtered readiness check observed
warm events for all 25 extras and 25 candidate-gate records: 21 were blocked
by `private_ack`, 4 by `public_book_stale`, and none were eligible at that
check. The private journal contains one successful aggregate subscription ACK
per venue and matched reseeds; that does not prove every OKX per-instrument
orders/positions ACK required by the entry gate. Startup logs show no reconnect
or NACK, and the existing logs do not expose per-instrument ACK outcomes, so
the 21 `private_ack` gates remain unresolved and fail-closed. Confirmed 1x
evidence removes only the leverage gate; private ACK, public-book, history, and
policy gates still control entry. No synthetic signal, forced order,
account-mode GET, or additional setting call was used after restart. Future
new extras remain ineligible until the same explicit preparation and runtime
readiness checks succeed.

## Per-instrument ACK handoff fix and failed startup — 2026-10-06

The earlier startup log had an aggregate OKX subscription success but no
per-instrument result. The bounded diagnostic run of PID `2357588` emitted one
matched orders ACK (KAITO) out of 108 expected before Bybit REST reseed failed.
The receive-path review found that the owner pump queued frames received before
`handshake_done`; after the handshake consumer took its first subscription ACK,
the remaining queued frames were not replayed to the private runtime.

B2.3 commit `57477ea` changes the existing owner-loop handoff to atomically set
`handshake_done` and drain only queued private frames through the existing
`handle_inbound_text` parser. Trade frames remain queued for their existing
consumer. The same handoff runs during reconnect. An offline three-coin startup
ACK-burst regression passed with all six orders/positions keys ready; with the
old boolean-only handoff restored temporarily, the test failed as expected.
Scoped validation passed 25/25 across the warm-loop, handshake-reseed ordering,
OKX subscribe, and Gear 2.3 hot-add tests. Syntax and diff checks passed. The
full warm-loop module also ran 29 tests: 27 passed, while two unrelated
websocket-connect tests errored because the local Python 3.14 `websockets`
module lacks the `connect` attribute those tests patch.

The f9e801d ACK-tracing deployment failed earlier at Bybit REST reseed. After
the queue-handoff fix was deployed as immutable package
`response-handler-20261006-gear23-B2.3-57477ea`, PID `2362496` produced 108/108
OKX ACKs: 54 instruments each had one matched `orders` and one matched
`positions` ACK, all `ack_ok=true`, request-correlated in generation 0. This
confirms the queued ACKs now reach the existing parser and per-instrument map.
The startup then failed at `warm_reseed_failed exchange=okx` before signal-loop
start. The existing reseed adapter intentionally reduces signed account,
position, and instrument GET outcomes to matched/inconclusive and did not record
the failing symbol, stage, HTTP status, or venue code. No underlying cause can
be claimed from this log. No orders were sent, and no leverage/account settings
were changed.

PID `2360461` was gracefully stopped from a fresh flat checkpoint before the
final `57477ea` attempt; PID `2362496` exited on the reseed failure. The checkpoint
remains flat and pending-free but names the now-stopped PID `2360461`, so it is
stale process identity, not proof of a running canary. The preserved open-window
deadline is `2026-10-09T12:27:03.428000Z`. No further startup retry was made.
The next required work is narrow, sanitized reseed-failure diagnostics (symbol,
probe stage, HTTP status and venue code only; no response payload or credentials),
then a reviewed, flat-guarded launch.
