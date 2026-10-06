# Synthetic roll exchange response handling

Trade acknowledgements remain separate from execution completion. An accepted
ACK records that the request was accepted; it does not complete the trade.
The waiter correlates private order updates to the current client order ID (and
IDs returned by that attempt's ACK), ignoring identifiable frames from older
attempts. A matching terminal Filled order update can complete even if it
arrives before its ACK.

For a live placement, both venues must report terminal Filled status, the
cumulative filled quantity must equal the quantity already sized and sent,
and the cumulative average price must be finite and positive. OKX uses
`state`, `accFillSz`, and `avgPx`; Bybit uses `orderStatus`, `cumExecQty`, and
`avgPrice`. Execution fragments, working states, zero or invalid fills,
quantity mismatches, cancellations, rejects, and timeouts do not mark the
intent open or closed. An incomplete result halts the synthetic roll and
leaves any possible exposure pending for review; this path does not retry or
send a recovery order.

Local simulation continues to use its signal-book fill prices without live
exchange quantity fields. The focused response tests use field-level synthetic
messages derived from the B0 run report; that report records statuses and
values but does not contain raw websocket payloads. Actual private websocket
payload capture and exchange execution timestamps remain unverified and are
outside this patch.

Architecture reference: [`architecture.md`](../architecture.md), Section 6.C
(`Live send`) describes this existing place path and its terminal completion
boundary; Section 8 (`B-private`) documents the related journal area.
