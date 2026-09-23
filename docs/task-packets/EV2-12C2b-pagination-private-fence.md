# EV2-12C2b — OKX pagination and private-generation fence

Status: local read-only probe and injected-response tests only. No VPS reads,
configuration changes, service restarts, trade socket or order sends. This
patch does not complete C2 or authorize K=1 publication/live startup.

## Pipeline block and existing modules

`app/bot/execution/signed_quantity_probe.py` remains bound to the durable WAL
candidate and its two REQUEST_SENT plans. The four signed reads now become
four **or more** when OKX pending orders span pages. The helper requests
`instType=SWAP&limit=100`, follows `after=<last ordId>` only for a decimal
order ID supplied by OKX, and accepts the concatenated response only after a
short page. It rejects malformed/rejected pages, duplicate IDs and a page
count above ten. The existing C1 comparator then checks all declared pool
orders, not only page one. The result records `okx_pagination_required=false`
only when pagination completed.

An optional `private_readiness` callback supplies `ReadinessSnapshot` from
the existing warm-session readiness model. Both private streams must be
ready with pause/kill clear before and after REST, and both reconnect
generations must match. Otherwise the probe fails closed. A successful
double sample records `private_generation_stable=true`, but
`private_reseed_required=true` remains because this callback is not yet
wired to a trusted runtime owner. Trade sockets
are neither required nor opened by this diagnostic probe.

## Design choice and remaining risks

Failing closed on a full unread page would be safe but would make accounts
with over 100 unrelated orders impossible to assess. Bounded pagination
preserves a complete order-pool check while limiting read amplification.
The OKX [pending-order API](https://www.okx.com/docs-v5/en/) defines `after`
pagination by order ID and a maximum page limit of 100. Pagination is not an
atomic snapshot: orders can change between pages, or between venue reads.
Even stable private generations do not prove exchange account UID ownership
or eliminate that race. Therefore `account_ownership_required=true`,
`snapshot_consistency_required=true`, and `publication_ready=false` remain
unconditional. Key fingerprints prove only which configured key was used.

Tests inject REST and readiness data; they cover second-page orders,
duplicate/malformed IDs, rejected pages, changed generation, a private
stream not ready, and a stable two-venue sample. Local tests cannot certify
VPS latency, actual exchange response shape, or UID. Before any isolated VPS
diagnostic, redact credentials/signatures/raw responses, verify release SHA,
and inspect collector plus continuing `would_sent` independently. Nothing
from this patch should be deployed to the active trading path.

Next: explicit UID/account-mode proof from exchange-backed read-only data,
then a coherent recheck protocol across changing orders/positions and
trusted warm-session ownership. B2c pending resolution, crash matrix,
fsynced lifecycle and a separate explicit live gate remain later steps.
