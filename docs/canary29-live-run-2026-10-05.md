# Canary29 live run — 2026-10-05

## Result

The isolated terminal-private runtime completed ten full open → terminal close → REST-flat cycles and stopped at the configured cap. The run was a foreground `python -m app.bot` process, not a systemd service. No production service or collector was changed.

| # | Coin / side | Filled qty (OKX contracts / Bybit units) | Open avgPx (OKX / Bybit) | Close avgPx (OKX / Bybit) |
|---:|---|---:|---:|---:|
| 1 | BICO long | 455 / 455 | 0.022 / 0.021995 | 0.02197 / 0.021993 |
| 2 | MUBARAK short | 1 / 100 | 0.0791 / 0.079177 | 0.079162 / 0.079186 |
| 3 | AZTEC short | 6 / 600 | 0.01744 / 0.01745 | 0.01748 / 0.01745 |
| 4 | RVN long | 402 / 4,020 | 0.002487 / 0.002488 | 0.00248 / 0.002486 |
| 5 | SIGN short | 8 / 800 | 0.012696 / 0.012651 | 0.012665 / 0.012596 |
| 6 | MUBARAK short | 1 / 100 | 0.076581 / 0.076608 | 0.076822 / 0.076829 |
| 7 | H long | 1.4 / 140 | 0.06951 / 0.06942 | 0.06931 / 0.06932 |
| 8 | MOVE short | 100 / 1,000 | 0.01001 / 0.01003 | 0.010064 / 0.01006 |
| 9 | RVN long | 395 / 3,950 | 0.002531 / 0.00253 | 0.002548 / 0.002554 |
| 10 | ZBT long | 115 / 115 | 0.08695 / 0.08674 | 0.08698 / 0.0869 |

Each terminal journal close reports matching cumulative quantities on both venues, and each cycle has a `canary29_cycle_flat` marker after the selected-symbol REST-flat check. The final marker is cycle 10, ZBT, at 2026-10-05 13:21:23 UTC; the VPS local timezone was verified as UTC. The process then exited. No final account-wide REST audit was run.

The campaign hit normal pre-send depth rejects without bypassing the gate. MOVE close was delayed by insufficient OKX close depth (four contracts available against the required 100) and closed at the next eligible roll. No order was sent on a rejected tick. There were no after-send halts or unknown fills.

## Timing and vector evidence

For all 20 open/close intents, native `StepChrono` monotonic markers provide these signal-decision-to-send measurements:

| Venue | Signal → queue enqueue, median / p95 / max | Signal → owner `ws.send` start, median / p95 / max |
|---|---:|---:|
| OKX | 2.165 / 10.876 / 12.071 ms | 2.947 / 12.771 / 15.173 ms |
| Bybit | 2.152 / 10.866 / 11.360 ms | 2.367 / 11.626 / 11.809 ms |

The first BICO open measured 12.071 ms to the OKX queue and 11.360 ms to the Bybit queue; owner send-start was 12.200 ms and 11.626 ms. Matching venue execution fields were present for all 20 intents: OKX `fillTime` in the trade journal, and Bybit `execTime` in private-wire frames correlated by the exact order ID/client order ID. Bybit `execId` deduplication and summed `execQty` matched the terminal journal quantity for all 20 intents; OKX terminal `accFillSz` matched for all 20. The first pair recorded Bybit `execTime=1791201536961` and OKX `fillTime=1791201536967`, 35 ms and 41 ms after the local signal wall timestamp. Across 20 samples, wall-signal-to-exchange-timestamp differences were Bybit 24/26/35/36 ms and OKX 30/32/42/47 ms (min/median/p95/max). These differences include host/venue clock offset and are not network-latency measurements.

The theta vector journal recorded all 29 pool coins during each of the ten held-position intervals. A complete 29-coin vector timestamp overlapped 8 of the 20 shorter `wait_fill` intervals; the other intervals ended between one-second vector emissions. This distinction is specific to the logged vector sampling cadence.

## Preparation and runtime locations

- Edited source: clean pre-B2.2 checkout `/private/tmp/arb_bots-canary29-20261005`, branch `codex/canary29-terminal-policy`.
- Tested/deployed source commit: `5cd295e`; deployed `app/bot/runtime.py` SHA-256: `498d597a361da24a237e883b8b8227b2e28d1f5b4e2991d067b85beb3f78e0f2`.
- VPS execution: `/root/venv/bin/python -m app.bot` from `/root/b-private-b-exp/response-manager-code/response-handler-20261005/`.
- Run root: `/root/b-private-b-exp/response-manager/20261005T-canary29-rerun2/`.
- Runtime logs and journals first materialized under that VPS-local run root. Durable remote/mounted-storage replication was not tested or claimed.
- Leverage preparation set and read back only the 26 newly authorized coins on both venues. The three previously confirmed coins were not queried or reset. The prep result and confirmed pool are in `/root/b-private-b-exp/response-manager/20261005T114149Z-response/`.
- A prior launch attempt stopped at constructor validation because the notional environment variable name was wrong; it exited before account/private work and sent no orders. The successful run used `BBOT_NOTIONAL_USDT=10`.

## Local verification and post-run edits

The runtime and manager changes were validated with Python compilation and focused Canary29 tests. The live-tested source hash above is the version used for all ten cycles. Subsequent local-only changes make the terminal-mode heartbeat read its manager slot, and attach `size_event=open|close` to no-send insufficient-depth journal rows; they were compiled and covered by one targeted close-reject test after the campaign. They were not run in another live session.
