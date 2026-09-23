# EV2-12C1 — native-quantity comparison for both venues

Status: pure parser/comparator and local tests. It consumes supplied REST
response objects but does **not** fetch or authenticate them, establish
freshness/account ownership, resolve the pending K=1 row, or unlock live
orders. The EV2 runtime live-start block remains in place.

## Pipeline block and existing files

```text
durable EV2 OPEN/CLOSE candidate + immutable instrument/pool mapping
  -> Bybit/OKX position and pending-order response parsing
  -> exact native quantity/side or zero-flat comparison
  -> result marked requires_signed_fresh_source
  -> [C2: signed fresh reads + ownership/plan binding]
  -> [B2c: pending resolution + K=1 lifecycle fsync]
```

`app/bot/execution/venue_quantity_compare.py` is new and pure. It compares
each candidate leg to the **same venue's** position quantity; Bybit linear
`size` and OKX SWAP `pos` are not compared to each other or converted by an
unrecorded multiplier. Bybit position/open-order `nextPageCursor` must be
present and empty. Any nonzero position in the declared 30-coin pool outside
the expected instrument fails closed. Any pool open order prevents a match.
Only Bybit one-way (`positionIdx=0`) and OKX net (`posSide=net`) are accepted
in this first narrow comparator; hedge modes need a separate reviewed rule.

Relevant primary specifications: [Bybit position info](https://bybit-exchange.github.io/docs/v5/position),
[Bybit open orders](https://bybit-exchange.github.io/docs/v5/order/open-order),
and [OKX positions API](https://www.okx.com/docs-v5/en/).

## Design choice and failure modes

Comparing only side/symbol, as the older restart checker does, can accept
the wrong amount. Comparing Bybit base size directly with OKX contract count
is also invalid. The selected exact per-venue comparison avoids both errors.
An incomplete Bybit page, extra pool exposure, unsupported mode, malformed
decimal, wrong side/quantity, or any pool open order fails closed.

This is **not** a signed reconciliation proof. The supplied JSON might be
stale, from the wrong account, or incomplete outside the checked Bybit
cursor. The instrument mapping is not yet bound to the durable order plan;
OKX response completeness and all 30-coin membership still need explicit
preflight. A `matched` result therefore retains
`requires_signed_fresh_source=true` and must never release K=1 or authorize
an order. If exchange-native amounts differ by a legitimate tolerance,
metadata and lot rules must be recorded and reviewed before relaxing exact
equality; do not silently round or guess.

## Local/VPS validation and next step

Local tests use temporary WAL files and response fixtures; no VPS request,
configuration change, private socket or order is made. Test the exact OPEN,
unilateral/partial exposure, extra pool position, pagination gap, open order,
unsupported mode, invalid quantity and both-flat CLOSE. The existing EV2
and K=1 replay tests must remain green. On an isolated target-VPS no-order
release, C2 will obtain signed fresh responses for the exact owned accounts,
verify instrument and account fingerprints, and compare against a frozen
30-coin order-plan manifest. Collector and `would_sent` must stay unaffected.

Next: EV2-12C2 signed/fresh/complete source and ownership binding, then
EV2-12B2c idempotent pending resolution and lifecycle publication with
crash fault injection. No live-canary approval follows from C1.
