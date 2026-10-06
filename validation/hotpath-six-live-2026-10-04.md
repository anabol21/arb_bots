# Hot-path live check — 2026-10-04

## Result

The hot-path timing logger worked end to end on the real warmed private-WebSocket route. The fixed runner injected six synthetic actions (open-short, close, repeated three times), sent twelve live order frames at exactly 5 XRP equivalent per leg, and persisted six `block=send_timing` rows with both Bybit and OKX monotonic markers. All six action results were terminal, and a final GET-only account check found both positions flat with no open orders.

## Measured boundary

The synthetic signal timestamp was captured immediately before calling `BotRuntime._synthetic_live_place`. `signal_to_ws_send_ms` ends at the warm owner-loop marker immediately before `websocket.send`; the queue marker is stamped after `asyncio.Queue.put` completes. These measurements cover the synthetic-signal-to-send hot path, including sizing, checks, signed frame construction, and queue handoff. They do not measure strategy signal generation or signal-to-exchange-fill time.

| Step | Action | Signal → `ws.send` start, Bybit | Signal → `ws.send` start, OKX | Queue → `ws.send`, Bybit / OKX | `ws.send` call, Bybit / OKX |
|---:|---|---:|---:|---:|---:|
| 1 | Open | 10.853 ms | 11.360 ms | 0.203 / 0.702 ms | 0.182 / 0.117 ms |
| 2 | Close | 3.300 ms | 3.938 ms | 0.226 / 0.855 ms | 0.253 / 0.112 ms |
| 3 | Open | 2.568 ms | 3.416 ms | 0.178 / 1.016 ms | 0.388 / 0.098 ms |
| 4 | Close | 2.168 ms | 2.538 ms | 0.173 / 0.534 ms | 0.184 / 0.088 ms |
| 5 | Open | 3.851 ms | 4.301 ms | 0.210 / 0.651 ms | 0.186 / 0.113 ms |
| 6 | Close | 3.248 ms | 3.797 ms | 0.202 / 0.743 ms | 0.225 / 0.107 ms |

Across six samples, signal-to-send-start was 3.274 ms median for Bybit (2.168–10.853 ms) and 3.868 ms for OKX (2.538–11.360 ms). Queue-to-send-start was 0.203 ms median for Bybit and 0.722 ms for OKX. The `ws.send` call itself took 0.206 ms median for Bybit and 0.110 ms for OKX. The first action was the outlier; the five later actions reached send-start in 2.168–4.301 ms. The timestamps do not isolate first-use import cost, so the outlier should not be attributed to imports alone.

The independent `signal_to_queue_ms` component was about 2.0–3.7 ms for actions 2–6; the first action was about 10.65 ms. The timing row contains the full sequence `queue_enqueued → dequeued → callback_started → ws.send start → ws.send return → callback_returned`, with all markers present for both venues in every action.

## Order and account checks

The frame guard accepted only Bybit quantity `5` and OKX size `0.05` (`ctVal=100 XRP`), with `reduceOnly=true` on each close. After each open, read-only position checks showed Bybit `+5 XRP` and OKX `−0.05` contracts; after each close both were zero. Approximate notional was $7.5 per leg at the observed XRP price. The final fresh GET-only preflight was `ready=true`, including flat positions, no open orders, the expected position modes, and 1x leverage.

One separate response/journal issue appeared: all six manager trade rows recorded OKX `fill_px=0`, while the subsequent account snapshots showed the expected fully matched positions and closes. Therefore `PlaceSendResult.latency_ms` and the persisted OKX fill price are not a valid measure of exchange execution time in this run. The hot-path send timestamps are independent and valid; the OKX acknowledgement/fill interpretation needs its own follow-up before relying on manager fill timing.

## Runtime and evidence

- Local edits are in this repository, including [the one-shot validation runner](run_hotpath_six_live.py), the timing propagation in `app/bot/private/{place_send.py,send_legs.py,step_chrono.py}`, and the sender markers in `ws_trivial_dual_leg.py` / `ws_warm_session.py` / `ws_warm_loop.py`.
- Execution used an isolated copy at `/root/b-private-b-exp/hotpath-code/hotpath-xrp-20261004T130026Z-4c9802f0`; the existing `spread-bbot-would-send-prod.service` stayed active and unchanged. The `gear22-live-canary` service remained inactive.
- The runtime wrote to `/root/b-private-b-exp/hotpath-six/hotpath-xrp-20261004T130026Z-4c9802f0/data`; `step_chrono.jsonl` has SHA-256 `353d09ed3f87552188de1284d8fa2b4024577a669b6da292137682861950db18`. It was first materialized on the VPS local filesystem and flushed with the existing fsync path; this experiment does not verify a remote-mounted or backup copy.
- The filtered run report has SHA-256 `c72fa2e6168f95c901f67696a3e9431c18c35a1a901ac76f920fc55c6491ff9c`. The final GET-only account evidence is `/root/b-private-b-exp/validation/account-postflight-hotpath-20261004T1309Z-4c9802f0.json`, SHA-256 `5779a17f8f1f9ac43f2aa2ae6e0cd8308f6728c40acc51625d342f69868b7c4b`; it reports `ready=true` and `orders_sent=0`.
- Local verification: 87 targeted unit tests passed (3 skipped); runner self-test, Python compilation, and `git diff --check` passed. The same 87 tests, compilation, and guard self-test also passed in the isolated VPS copy before execution.

## Signal-to-exchange full-fill timing

Read-only Bybit and OKX execution-history GETs matched each of the six action intents to the exact order ID and client ID in its correlated acknowledgement. The unique execution rows total the submitted size on both legs for all six actions: 5 XRP on Bybit and 0.05 OKX contracts (5 XRP equivalent). The table shows the venue execution timestamp and the difference from the VPS `signal_ts_ms`; for every action, first execution and full-fill timestamps were the same millisecond.

| Action | Signal (UTC) | Bybit full fill (UTC / delta) | OKX full fill (UTC / delta) | Both legs full |
|---:|---|---|---|---|
| 1 open | 13:09:04.554 | 13:09:04.599 / 45 ms | 13:09:04.595 / 41 ms | yes |
| 2 close | 13:09:04.907 | 13:09:04.932 / 25 ms | 13:09:04.940 / 33 ms | yes |
| 3 open | 13:09:05.298 | 13:09:05.321 / 23 ms | 13:09:05.331 / 33 ms | yes |
| 4 close | 13:09:06.666 | 13:09:06.690 / 24 ms | 13:09:06.698 / 32 ms | yes |
| 5 open | 13:09:07.041 | 13:09:07.066 / 25 ms | 13:09:07.075 / 34 ms | yes |
| 6 close | 13:09:08.407 | 13:09:08.432 / 25 ms | 13:09:08.442 / 35 ms | yes |

Across six actions, signal-to-full-fill was 25 ms median for Bybit (23–45 ms) and 33.5 ms for OKX (32–41 ms). The time until both legs were fully filled was 33.5 ms median (32–45 ms). Bybit actions 1 and 3 were fragmented across two unique executions each; both fragments were included in the exact 5 XRP total. These are wall-clock differences between VPS-generated signal timestamps and venue execution timestamps. The VPS-to-exchange clock offset was not independently calibrated, so the deltas include any clock offset. The filtered per-action evidence is [hotpath-six-live-2026-10-04-execution.json](hotpath-six-live-2026-10-04-execution.json); it stores no order IDs, credentials, or raw account payloads. The Bybit response also indicated another history page, so the evidence makes a completeness claim only for the six exact matched orders, not for unrelated XRP activity in that query window.
