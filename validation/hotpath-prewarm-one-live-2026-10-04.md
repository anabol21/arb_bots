# Startup prewarm live check — 2026-10-04

**Contour:** `synthetic_roll` only: `BotRuntime.run()` → `_prepare_synthetic_live_sender()` → `_synthetic_live_place()` → `place_send.place_live()` → `send_long` / `send_short` → `TrivialDualSender` → the warm Bybit and OKX owner-loop `websocket.send` calls. This does not cover `gear22_live_canary` / `LiveBroker.place`.

**Startup result:** On the isolated VPS code copy, the private session was ready with `handshake_count=1`; startup imported the place modules and prepared the cached sender before public signal tasks. Both consumers were ready at queue depth zero, `frames_enqueued=0`. One 5-XRP-per-leg open and one reduce-only close completed; GET checks found Bybit `+5 XRP` / OKX `−0.05` contracts after open, then both flat with no open orders.

| Action | Bybit signal → queue / `ws.send` | OKX signal → queue / `ws.send` |
|---|---:|---:|
| Open | 2.147 / 2.303 ms | 2.155 / 2.830 ms |
| Reduce-only close | 2.033 / 2.278 ms | 2.044 / 2.785 ms |

**Evidence:** Code executed from `/root/b-private-b-exp/hotpath-code/hotpath-xrp-20261004T140659Z-4fcedacb`. Filtered VPS report: `/root/b-private-b-exp/hotpath-six/hotpath-xrp-20261004T140659Z-4fcedacb/report.json`, SHA-256 `794762c3aca393cf41608a95c70cf2b35a8110c0cb3043a20425cdb70d6926b9`; step chronometry SHA-256 `01ce59f12edf9a14a3ac3ec62c604c754ab14b0c2551cd2393d83395acb6769a`. Runtime artifacts first materialized on VPS-local storage; this run did not verify a mounted or remote copy.

**Limit:** This is one isolated run, not a latency guarantee. OKX manager `fill_px=0` remained anomalous, so those fields were excluded from execution evidence. The send markers establish queue and owner-loop `ws.send` boundaries, not signal-generation or fill latency.
