# B-private — default live send is trivial dual-leg (Contour B)

Track: glue / B-private. Code only. **No VPS or live deploy in this change.**

Staging A/B (human, 2026-09-04) measured:

| Contour | signal→first ws.send | notes |
|---------|----------------------|--------|
| A — full W6 manager | ~2269–2609 ms | ~340 ms leg skew; Bybit ack ~385 ms |
| B — queue→ws.send | ~0.7–1.1 ms | ack ~38/55 ms; signal→venue_fill ~20/28 ms |

W6 recover / operator_approval / lease / `prepare_approved` + journal fsync /
preflight on the signal→send path is unacceptable for the live manager.

---

## What sits on the hot path now

After strategy filters in `LiveBroker.place` (kept):

1. coin / qty / open vs close / `already_in_position` / `held_coin` / `k_live`
2. build two **signed** place frames with W6 builders
   (`build_bybit_trade_place`, `build_okx_trade_place`) — reqId + HMAC +
   `orderLinkId`, OKX `instIdCode`
3. dual `queue.put` → long-lived sender `ws.send` on **both** legs
   (`TrivialDualSender.enqueue_dual`)
4. no wait-for-fill before the other leg

Warm private+trade WS is already up (`PrivateWarmSession`, process lifetime).
`place_io_section` is a lock so keepalive does not steal ACK frames — not a
multi-second pre-send check.

## What moved off the hot path

| Was on W6 signal→send | Now |
|-----------------------|-----|
| `_recover_inflight_w6` | not called on default path |
| `ApprovalVault.issue` / consume | not on default path |
| lease `assert_can_send` | not on default path |
| `prepare_approved` + journal fsync / preflight before `ws.send` | not on default path |
| sequential per-leg prepare | both frames built, then dual put |

Ack / fill / flatten / reconcile stay **after** send (observe). Do not add
new waits that gate the contour.

The standing **wire transcript** records every private+trade send/recv with
`wall_ms` / `mono_ns` so signal→send, send→ack RTT, and
`fill_delivery = local_recv − venue_ts` can be derived without putting a
fill gate on this path. Layout and redaction:
[`docs/b-private-wire-transcript.md`](b-private-wire-transcript.md).

## How to flip back to W6

Default (no extra flags beyond live send):

```text
BBOT_BROKER=private_live
VENUE=live
LIVE_ORDERS=1
# BBOT_PRIVATE_SEND_PATH unset or trivial
# BBOT_PRIVATE_W6 is NOT required
```

Old manager (safety experiments only):

```text
BBOT_PRIVATE_SEND_PATH=w6
BBOT_PRIVATE_W6=1
```

W6 remains TRUMP-profile + recover/approval/lease. `BBOT_PRIVATE_W6=1` alone
does **not** switch `default_live_send_pair`. Classic W6/W7 CLI
(`--ws-w6-dual-leg` / `--ws-w7-parallel-dual-leg`) is unchanged.

OKX `instIdCode` must be cached **before** a signal (place is cache-only):

```text
BBOT_OKX_INST_ID_CODES=BTC-USDT-SWAP:…,ETH-USDT-SWAP:…,SOL-USDT-SWAP:…,XRP-USDT-SWAP:…
```

or `LiveBroker.warmup_inst_id_codes(symbols, fetch_fn=...)` at process start.
A matching warm-session `okx_runtime.okx_inst_id_code` is a last-resort cache.

## Modules

| Path | Role |
|------|------|
| `app/bot/private/ws_trivial_dual_leg.py` | path resolve, signed frames, dual sender |
| `app/bot/private/live_broker.py` | filters + `default_live_send_pair` |
| `app/bot/private/ws_gates.py` | `assert_ws_trivial_dual_leg_gates` (no W6 flag) |
| `app/bot/broker.py` | `BBOT_BROKER=private_live` |

## Tests (local)

```bash
PYTHONPATH=. python3 -m unittest tests.test_trivial_dual_leg -v
PYTHONPATH=. python3 -m unittest tests.test_bbot_gear2 tests.test_warm_ws_place_threadsafe -q
```

Staging re-measure is a later explicit user go. This PR does not restart
gear2 or mutate VPS.

## Out of scope

- VPS deploy / live gear2 restart / SOL leftover flatten
- Gear2 strategy thresholds
- New fill-wait gates on the contour
- Changing W6/W7 CLI defaults
