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
