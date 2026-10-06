# Synthetic roll signal-to-send readiness — 2026-10-04

## Pipeline block

Frozen contour under review: `BotRuntime._synthetic_live_place` →
`place_send.place_live` → `send_long` / `send_short` → signed Bybit and OKX
frames → `TrivialDualSender.enqueue_dual` → warm connector → owner-loop
`websocket.send` on both venues. This is the `synthetic_roll` profile; the
`gear22_live_canary` / `LiveBroker.place` path is separate.

## Existing code and startup state

Private transport warmup already runs before the public books and synthetic
roll tasks. With live gates armed, `BotRuntime.run()` awaits private WS startup,
then prefetches OKX contract metadata and instrument codes and sets 1x leverage
for configured coins before creating signal-facing tasks. The private session
performs its handshake and post-handshake application heartbeat and fails
startup if it is not ready (`app/bot/runtime.py:1558-1582`,
`app/bot/private/ws_warm_session.py:881-900,1289-1295`).

Import warmup is only partial. `ws_trivial_dual_leg` is already loaded while
`BBOT_BROKER=private_live` constructs `LiveBroker` (`app/bot/broker.py:64-74`,
`app/bot/private/live_broker.py:48-60`). The synthetic callback still imports
`place_send` and `send_legs` on its first invocation
(`app/bot/runtime.py:502-515`; `place_send.py:29-40`). It also creates its own
`TrivialDualSender` on that first action; construction starts the sender thread,
event loop, and queues (`app/bot/runtime.py:542-545`,
`app/bot/private/ws_trivial_dual_leg.py:306-330`). Therefore the transport is
warm before signals, but all synthetic send-path setup is not.

## Evidence and limits

The six-action live run reached owner-loop `ws.send` start in 2.168–4.301 ms on
actions 2–6; the first action measured 10.853 ms for Bybit and 11.360 ms for
OKX. The run validates this real warmed-WS route and its measured boundary for
that run. It does not isolate import cost: first-action time also includes
first-use sender setup, and there are no per-stage import markers. Do not
attribute the first-action outlier to imports alone. The validation report
records the order/account checks and the separate OKX `fill_px=0` journal
problem; send-start timing is not fill timing
([report](../validation/hotpath-six-live-2026-10-04.md)).

## Startup preparation patch

`BotRuntime.run()` now performs preparation only when the exact synthetic live
gates are enabled and the private session reports ready. After metadata,
instrument-code, and leverage warmup, it imports `place_send` and
`send_legs`, creates and caches a `TrivialDualSender` bound to that same
session, and completes this work before creating signal-facing tasks. Sender
readiness waits until both consumer coroutines have reached their queues;
startup verifies both queue depths are zero and logs `frames_enqueued=0`.
Warmup does not enqueue an item or call `ws.send`.

`_synthetic_live_place` now requires the cached sender to be ready and bound to
the active warm session; it no longer creates sender threads on first use.
Startup failure and normal shutdown close the sender before stopping the warm
session. This readiness check verifies local queue consumers and session
identity; it does not validate order-frame construction or venue acceptance.

## One open/close live result

See the [one-cycle VPS report](../validation/hotpath-prewarm-one-live-2026-10-04.md) for the exact startup contour, timings, and artifact hashes.

The 2/2 focused no-send tests passed in the VPS virtualenv. One isolated live
run passed a fresh GET-only preflight (all gates true, XRP flat, no open orders),
then prepared the cached sender on the same warm session (`handshake_count=1`,
both queues ready at depth zero, no frames enqueued) before public tasks. One
5-XRP-per-leg open and one reduce-only close both verified; GET checks showed
Bybit +5 XRP and OKX −0.05 contracts after open, then both flat with no open
orders after close. The OKX fill-price field remained anomalous, so manager fill
fields were excluded from execution evidence; these two timings describe only
this run and do not establish a universal latency bound.

| Action | Bybit signal → queue | Bybit signal → `ws.send` | OKX signal → queue | OKX signal → `ws.send` |
|---|---:|---:|---:|---:|
| Open | 2.147 ms | 2.303 ms | 2.155 ms | 2.830 ms |
| Reduce-only close | 2.033 ms | 2.278 ms | 2.044 ms | 2.785 ms |

Filtered VPS report: `/root/b-private-b-exp/hotpath-six/hotpath-xrp-20261004T140659Z-4fcedacb/report.json` (SHA-256 `794762c3aca393cf41608a95c70cf2b35a8110c0cb3043a20425cdb70d6926b9`). Step chronometry SHA-256: `01ce59f12edf9a14a3ac3ec62c604c754ab14b0c2551cd2393d83395acb6769a`.

## Validation boundary

The 2/2 no-send tests verified import preparation, ready and empty queues, no
send callback, refusal when the session is unready, and sender-before-session
cleanup. The one-cycle live run exercised the actual `BotRuntime` preparation
helper and `_synthetic_live_place` route. These checks do not verify a remote
mounted or backup copy of the VPS-local evidence files.

Runtime evidence came from the isolated VPS copy and VPS-local data root above;
it does not establish correctness of a mounted or remote storage copy.
