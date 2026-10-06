# Gear 2.3 live canary launch report — 2026-10-06

## Deployment result

The Gear 2.3 B2.3 canary is running as PID `2343195` from immutable commit
`e2e134ca54731af95f1a8ea9594372e872fda025` (branch `B2.3`). The B2.3 branch
includes the validated preB2.2 contour, Gear 2.3 patches A/B, and the reviewed
close-PnL correction. `main` is `1d43137d208d4921ddafb406e6d267c861b3d318`;
it retains the separate collector and would-send production contour.

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

The run started at `2026-10-06T12:27:02.937Z` and its 72-hour open window ends
at `2026-10-09T12:27:02.937Z` (15:27:02 MSK). The persistent canary state
currently reports `position={}`, `pending=false`, `halt=null`, and
`completed_cycles=0`. The configured limits remain K=1, notional 10 USDT,
policy `gear22_frozen_v1`, `canary_max_cycles=0`, and 72 hours. It uses the
existing natural signal and halt rules; no synthetic signal or forced order was
issued. The process starts with the original 29 coins carrying existing
confirmed-1x evidence. No leverage or account settings were changed.

## Readiness evidence at initial review

At startup, `private_warm_started` reported `ready=True`,
`handshake_count=1`; the startup flat check confirmed 29 coins. Heartbeats from
both Bybit and OKX trade sockets continued after warm readiness. After the
snapshot was linked, all 25 extras were registered. Public ACKs were observed
for all 54 coins on both venues: 54 Bybit `orderbook.1` and 54 OKX `books5`
subscriptions. Observer warm events covered 25 extra coins. Candidate gate
events showed all 25 extras blocked for missing prep-confirmed 1x; per the
runtime gate ordering, an extra reaching this reason has passed its
instrument-scoped private readiness checks at that evaluation. Public
`stale`/`incomplete` reasons were also present while books were settling, so
this report does not claim all extras are currently entry-ready. The 25 extras
remain entry-ineligible until every gate passes. No per-coin raw private ACK
count is claimed because those ACKs are tracked in memory rather than journaled
as an independent per-coin summary.

No order or leverage mutation was attempted. The live order path remains
enabled only for natural policy decisions under the existing guards; no
qualifying signal had been observed at this initial review. The standalone
would-send process is separate and continues unchanged.

## Monitor and stop conditions

Read the single PID, log, and state above for runtime health. Confirm state is
flat and `pending=false` before any operator restart. A held position or
pending terminal action must resolve through the existing natural close path;
do not force-close or kill the process. Extra pool coins missing 1x evidence
are expected to remain blocked. Do not set leverage or change profile, K,
notional, selector, or halt settings as part of this canary.

For rollback, first require the checkpoint to show `position={}` and
`pending=false`, then stop PID `2343195` gracefully before deploying the prior
source. If a position is held or an action is pending, preserve the current
source and journal and use only a compatible handoff after the existing
natural close completes. Never force-kill or auto-flatten during rollback.
