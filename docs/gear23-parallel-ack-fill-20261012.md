# Gear 2.3 parallel ACK/fill reads (2026-10-12)

## Change

The synthetic-roll place path now starts one blocking fill reader per venue in
a two-worker `ThreadPoolExecutor`. It submits both readers before collecting
either result, then processes the results on the original place worker in the
existing deterministic OKX-then-Bybit order. Leaving the executor scope joins
both readers before the runtime releases its existing `place_io_section`.

The runtime still computes one absolute 60-second fill deadline before place
begins. Send time therefore consumes the fill budget, and both receiver
callbacks use the same deadline. This patch does not change the deadline's
location, result classification, send path, journal writer, retry policy, or
pending/recovery behavior. The per-venue receive callbacks read distinct
venue-owned warm-runtime queues; this adds no direct websocket reads. Journal
rows and step chronometry remain on the original place worker.

If one receiver raises, the exception continues to propagate to the existing
caller. The executor context waits for its sibling reader to finish before
propagating it, so the existing runtime context releases only after both
readers have exited. The already-written `pending` row remains the durable
record; this patch does not infer a fill or clear pending state.

## Validation

Local tests cover concurrent reader progress despite OKX-first collection,
same-deadline asymmetric fill retention, original-thread journal writes,
exception propagation and sibling-reader cleanup. The shared-deadline
regression starts its test deadline at `wait_fill` entry and makes both
callbacks return immediately when that deadline expires, as the runtime
receiver does. It passes with parallel reads and fails against the old serial
loop (`fill_timeout` instead of the expected `asymmetric_fill`). Existing
synthetic-roll tests continue to cover paired fills, stale order IDs, venue
rejects, partial terminal updates, and quantity mismatch. These are local
checks only; they do not establish VPS behavior or venue connectivity.

130 scoped local tests passed without skips; the affected private suite was
rerun after strengthening its common-deadline regression (44/44 passed).
Independent L1 review found no blocking concurrency/lock/lifecycle defect.

Deployment is limited to `place_send.py` in the stopped Gear 2.3 checkout.
The existing launcher and Sentry environment file are reused unchanged. Before
restart, fresh GET-only positions/open-orders evidence must show the campaign's
Bybit USDT linear and OKX SWAP products flat. The known RSR pending close may
then be resolved by an appended account-flat reconciliation marker and a backed-
up checkpoint reset. That marker resolves the account slot, does not erase the
original pending row, and does not invent fill price, quantity or execution time.
Timeout/partial/asymmetric still halt; automatic order cancellation is not added.

## VPS canary rollout result

The decision-context baseline was merged through PR #81; main merge commit
`e0816c44c5cecd90375d93f41a15128747d8cf68`. The separate parallel-reader patch
is commit `156d8e6a5f5f12faa72d5e8171620b4749124d26` in draft PR #82.
Only its `app/bot/private/place_send.py` was overlaid on the stopped deployed
Gear 2.3 code; this is not a full checkout of current main. The previous file
and canary checkpoint were backed up before modification.

On VPS Python 3.10.12 / websockets 16.1, the affected private suite passed
44 tests. No exchange calls occur in those tests. A single fresh GET-only
pre-start check found zero nonzero Bybit USDT-linear / OKX SWAP positions and
zero open orders. The RSR timeout close was reconciled with an appended marker
and checkpoint reset. Historical pending rows remain intact; precise execution
prices/timestamps of that close were not reconstructed.

The existing launcher started PID `3593028` at 2026-10-12 15:32:26.868 MSK.
At 15:35:12 MSK, the process was alive, private warm readiness was true, the
64-coin pool had accepted 36,143 ticks, pending was false, the slot was empty,
and the execution halt reason was null. All 35 hot-add extras had confirmed
1x; TW and theta files had been updated less than one second before the check.
The existing unlimited-cycle / unlimited-time campaign settings were preserved.

The launcher checksum was unchanged and the process received the existing
SENTRY_DSN / SENTRY_ENVIRONMENT configuration. No Sentry policy or event code
was modified. Delivery to the downstream Grok bot was not exercised; an explicit
Sentry initialization log was not present in the checked startup logs.

No extra experiment orders were submitted. At this startup observation there
was no new live trade validating the modified receiver path. This evidence
confirms startup and mock concurrency behavior, not a completed real dual-leg
trade under the patch. Timeout, partial, or asymmetric results still halt the
manager and retain pending evidence; automatic cancellation is not implemented.

Operational evidence is local to the VPS under
`/root/b-private-b-exp/response-manager/gear23-ack-fill-parallel-20261012/`:
`account-flat.json`, `launch.json`, `startup-result.json`, and pre-change backups.
No env files or credentials are included in the PR.
