# WAL/EDEN canary dual-leg latency — 2026-09-08

Track 3 / B-private. Engineering record of the Contour B canary overnight
run that stopped after 10 successful `dual_ack=both_accepted` samples
(2026-09-08). This is not a profitability claim, not a Gear-2 close stamp,
and not a VPS deploy.

Chronometry contract: [`canary-trade-chronometry.md`](canary-trade-chronometry.md).
Contour: [`canary-wal-eden-contour.md`](canary-wal-eden-contour.md).
Matcher / 64003 fixes landed on main in [#37](https://github.com/anabol21/arb_bots/pull/37).

Primary metric for Mikhail: **send→fill**.

## Setup

| Item | Value |
|------|--------|
| Profile | `canary_wal_eden` |
| Coins | WAL, EDEN |
| Send path | Contour B trivial dual-leg |
| Notional | $10 / leg |
| Depth gate | on (pre-signal, unchanged contour) |
| `avg_window` | 10s |
| `open_frac` | 0.7 |
| Stop rule | 10 successful `dual_ack=both_accepted`, then stop unit |
| Data root (ops context only) | `/data/bbot-canary-wal-eden` |

EDEN hourly p75 experiment thresholds (overlay for this run; not the
contour-doc default of `0.1` on all four):

| Gate | Threshold |
|------|-----------|
| `open_long` / `close_short` | `0.089` |
| `open_short` / `close_long` | `-0.160` |

The 10 dual-leg samples below are **EDEN**. No WAL latency table is
recorded here.

## Bugs fixed during the run (main via #37)

Two independent matching/subscribe bugs showed up on this overnight.
Both are on main via [#37](https://github.com/anabol21/arb_bots/pull/37).
This report does not re-litigate the patches.

1. **OKX VIP `fills` → `64003` reconnect storm.** After
   [#35](https://github.com/anabol21/arb_bots/pull/35) the private OKX
   session subscribed VIP `fills`. The venue returned `64003`. The
   runtime treated that as `auth_reject` and ~10s private reconnect
   storms followed; EDEN orders were not stably subscribed. Fix: do not
   subscribe `fills`; treat `64003` as a subscribe nack, not auth
   failure. Chronometry already reads `fillPx` / `avgPx` / `uTime` from
   `orders`.

2. **OKX `send_to_fill.okx=null` despite fills on the wire.** Even with
   `orders` carrying `fillPx` / `fillTime`, chronometry left
   `send_to_fill.okx=null`. OKX `clOrdId` is `o` + the first 31 hex of
   the 32-hex `dual_leg_id` (32-char venue cap). Match required full-id
   containment, so the truncated id missed. Fix: client-id overlap
   match (containment both ways on compact hex and on the tail after a
   1–2 letter prefix). See
   [`canary-trade-chronometry.md`](canary-trade-chronometry.md)
   (“Intent ↔ venue client-id match”).

The original live dashboards were written **before** the matcher fix.
An offline rebuild of the six **post-hotfix** samples (5–10) recovered
OKX venue send→fill. Rebuild procedure is in
[`canary-trade-chronometry.md`](canary-trade-chronometry.md)
(“Rebuild an existing report”).

## Original live chronometry (at stop)

As reported when the unit hit 10/10. OKX send→fill was often `null`
because of the `clOrdId` match bug, not because the venue omitted fills.

Times are milliseconds. `B/O` = Bybit / OKX. Intent ids are 8-char
prefixes. `—` means the live report did not give a number (Bybit).
`null` means chronometry recorded no fill for that venue.

| # | intent_id | side | signal→send B/O ms | send→ack B/O ms | send→fill B/O ms |
|---|-----------|------|--------------------|-----------------|------------------|
| 1 | `b88d70c2` | close | 2/2 | 47/54 | —/null |
| 2 | `def0face` | open_short | 38/125 | 185/97 | —/null |
| 3 | `9f1c9282` | close | 2/3 | 37/55 | 24/null |
| 4 | `2f5b1331` | open_long | 2/10 | 42/121 | null/null |
| 5 | `95790b7c` | close | 3/3 | 50/55 | 35/null |
| 6 | `9ae7ae67` | open_short | 2/3 | 39/55 | 24/null |
| 7 | `bcb554a1` | close | 2/2 | 35/55 | 22/null |
| 8 | `e935c3f9` | open_long | 2/2 | 34/55 | 22/null |
| 9 | `224958b8` | close | 1/2 | 37/55 | 24/null |
| 10 | `f15bac10` | open_short | 277/767 | 690/500 | −77/null |

## Rebuilt send→fill (samples 5–10)

Venue `fillTime`-based send→fill after the `clOrdId` overlap match.
Samples 1–4 were not rebuilt. Bold is the recovered OKX value that was
`null` in the live table.

| intent | send→fill Bybit / OKX ms |
|--------|--------------------------|
| `95790b7c` | 35 / **29** |
| `9ae7ae67` | 24 / **28** |
| `bcb554a1` | 22 / **29** |
| `e935c3f9` | 22 / **29** |
| `224958b8` | 24 / **28** |
| `f15bac10` | −77 / −59 |

`f15bac10` is an overlapping/slow-path outlier (same row as #10 in the
live table: signal→send 277/767 ms, send→ack 690/500 ms). Negative
send→fill is recorded, not discarded; do not fold it into the clean
range.

## Clean-sample takeaway

Prefer samples **3, 5–9**. Exclude **#10** (`f15bac10`). Samples 1, 2,
and 4 are left out of the clean range: 1–2 lack a Bybit send→fill in
the live report; 4 has `null/null` live send→fill; 1–4 were not in the
offline rebuild.

On that clean set:

- **signal→send:** ~1–3 ms both venues
- **send→ack:** Bybit ~34–50 ms, OKX ~55 ms
- **send→fill Bybit:** ~22–35 ms (venue `execTime`)
- **send→fill OKX:** ~28–29 ms after the match fix (venue `fillTime`).
  Live reports before the matcher fix showed this as `null`.

Local WebSocket delivery of OKX `orders` updates can lag 2–6 s.
Chronometry correctly uses venue time when the client id matches.

n = 6 dual-legs in the preferred set (3, 5–9); 5 of those have rebuilt
OKX send→fill. This is a latency measurement on one overnight EDEN
canary, not a distribution over days or coins.

## Ops outcome

- Unit stopped after **10/10** `dual_ack=both_accepted`.
- Gear-2 was not started or changed.
- Venue position was **not** auto-flattened. Local/venue state left
  `open_short` EDEN at stop. That is an ops leftover, not a strategy
  conclusion.

No claim is made here about WAL, about thresholds other than the EDEN
hourly p75 overlay above, or about live-size / Gear-2 contours.
