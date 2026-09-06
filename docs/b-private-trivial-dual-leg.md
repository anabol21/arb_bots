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
   `orderLinkId`, OKX `instIdCode`. OKX trade WS message `id` is sanitized
   at the frame boundary to alphanumeric ≤32 (`sanitize_okx_ws_id`).
   Journal `new_opaque_id("req")` (`req_<hex>`) is **not** legal on OKX
   (`60033` / `Parameter id error` on canary 2026-09-05); Bybit `reqId`
   may still use `prefix_`.
3. dual `queue.put` → long-lived sender `ws.send` on **both** legs
   (`TrivialDualSender.enqueue_dual`)
4. no wait-for-fill before the other leg
5. **after** both sends: short dual trade-socket ACK wait (default 2s,
   `BBOT_PRIVATE_ACK_TIMEOUT_SEC`). Local `open_long` / `open_short` /
   close-clear only if **both** Bybit `retCode` and OKX order/`event=error`
   ACKs succeed. One-leg reject or ACK timeout → stay / return **flat** and
   reduce-only flatten any accepted (or timed-out) **open** leg.

Warm private+trade WS is already up (`PrivateWarmSession`, process lifetime).
`place_io_section` is a lock so keepalive does not steal ACK frames — not a
multi-second pre-send check. ACK wait is **after** send, inside that lock.
It is not a pre-signal gate and not a fill_delivery wait.

## What moved off the hot path

| Was on W6 signal→send | Now |
|-----------------------|-----|
| `_recover_inflight_w6` | not called on default path |
| `ApprovalVault.issue` / consume | not on default path |
| lease `assert_can_send` | not on default path |
| `prepare_approved` + journal fsync / preflight before `ws.send` | not on default path |
| sequential per-leg prepare | both frames built, then dual put |

Fill / reconcile stay **after** send (observe). The only post-send wait on
Contour B is the short dual **trade ACK** (accept/reject/timeout), which
gates local position — not venue fill. Do not add a fill-wait gate.

Canary (WAL/EDEN) additionally freezes a public L1 ring and writes a
chronometry HTML page **after** both ACKs — see
[`canary-trade-chronometry.md`](canary-trade-chronometry.md). That work is
not on signal→send.

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
| `app/bot/private/dual_leg_ack.py` | post-send dual ACK wait + flatten venue set |
| `app/bot/private/live_broker.py` | filters + `default_live_send_pair` + ack-aware position |
| `app/bot/private/ws_gates.py` | `assert_ws_trivial_dual_leg_gates` (no W6 flag) |
| `app/bot/broker.py` | `BBOT_BROKER=private_live` |

## Tests (local)

```bash
PYTHONPATH=. python3 -m unittest tests.test_okx_ws_message_id tests.test_trivial_dual_leg tests.test_dual_leg_ack -v
PYTHONPATH=. python3 -m unittest tests.test_bbot_gear2 tests.test_warm_ws_place_threadsafe -q
```

Staging re-measure is a later explicit user go. This PR does not restart
gear2 or mutate VPS.

## Out of scope

- VPS deploy / live gear2 restart / SOL leftover flatten
- Gear2 strategy thresholds
- New fill-wait gates on the contour (ACK-only; fills stay observational)
- Changing W6/W7 CLI defaults
- VPS deploy of the ACK-aware position change (code + unit tests only)

Overnight canary 2026-09-05 (EDEN, OKX `60033`) left Bybit filled and local
`open_long` because Contour B marked position after `ws.send`. ACK-aware
position is now on this path: that reject returns `dual_ack_rejected:okx:60033`,
keeps local flat, and flatten-closes the accepted Bybit leg. The OKX
alphanumeric `id` sanitize remains the venue-side prevention of that
specific `60033`.
