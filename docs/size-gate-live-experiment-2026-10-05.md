# OKX size-gate correction and bounded live experiment — 2026-10-05

## 1. Pipeline block

The private theta manager receives OKX `books5` sizes in contracts and planned order sizes in base-coin quantity. The previous comparison mixed those units. The private path now converts available OKX contracts to base quantity using the existing startup-cached positive `ctVal`; missing or invalid `ctVal` fails closed. The public WS parser remains in raw contract units and Bybit depth is unchanged. The existing mapper still builds orders in venue units, and live closes use the stored open fills with `reduceOnly=true`.

## 2. Existing files and modules involved

- `app/bot/theta_trade_manager.py`: dimensionally correct private OKX depth check; close depth requirement converted from stored OKX filled contracts to base quantity.
- `app/bot/runtime.py`: private manager context attaches cached `ctVal` to the runtime book; parser output remains untouched.
- `validation/run_response_manager_experiment.py`: candidate prefilter uses the same unit conversion; bounded runner diagnostics and plan initialization fixed.
- `tests/test_okx_depth_units.py`: focused regression for conversion, missing/invalid multiplier, non-private behavior, and close sizing.

## 3. Candidate interpretations

The evidence supports a unit mismatch: derivative book depth is a contract count, while the plan was compared in base quantity. Applying `ctVal` at the private manager boundary fixes that dimensional comparison without changing parsing or order sizing.

## 4. Key risks and failure modes

Missing or malformed metadata blocks the private gate. Close depth is compared against the actual filled quantity, with OKX stored contracts converted through the same `ctVal`. This experiment validated these behaviors for the approved instruments; it does not establish broader venue or market behavior. The initial chronometry extraction missed `step_chrono.jsonl` and the venue execution timestamp fields; those markers were present in the run artifacts.

## 5. Minimal patch and validation

The code was edited in isolated checkout `/private/tmp/arb_bots-exchange-response-20261004` and transferred to the authorized VPS experiment directory. Focused checks passed:

- `python3 validation/run_response_manager_experiment.py --self-test` — pass, zero orders; includes a local mocked sender-wrapper path through quantity preflight, plan append, and reservation counters.
- Close-unit regression: 2 OKX contracts × `ctVal=10` requires 20 base units; 2 available contracts passes, while 1 available contract (10 base) fails.
- Scoped `py_compile` of manager, runtime, and runner; `git diff --check` — pass.

## 6. VPS and storage validation

Execution used `/root/venv/bin/python` in `/root/b-private-b-exp/response-manager-code/response-handler-20261005/`, with the existing production research tree only as a secondary import path. Imported module paths, hashes, and the `size_check` signature were verified before the final run. The successful final run disabled repeated account-mode, leverage, instrument-configuration, and wallet audit GETs and setters, reusing the previously confirmed 1x leverage. An earlier preparatory run still used the old GET preflight and leverage readback; those checks were removed before the successful run.

The completed run is `20261004T222519Z-response`, report path `/root/b-private-b-exp/response-manager/20261004T222519Z-response/result.json`. Runtime journals and logs first materialized under that VPS-local run directory (`data/`, `private-data/`, and `bbot-experiment.log`). Step markers were read from `/root/b-private-b-exp/response-manager/20261004T222519Z-response/data/theta_trades/event_date=2026-10-04/step_chrono.jsonl`. No mounted or remote durable copy was validated. The attached summary below omits raw wire frames and identifiers.

## 7. Success criteria and result

The bounded experiment completed three cycles: six dual-leg intents and 12 order requests. Each open and close reached terminal full fills; journal quantities and prices were read back; close quantities matched the stored open fills with `reduceOnly=true`; a REST-flat projection passed after each close. The initial cycle's bounded raw/reordered terminal replay, duplicate ACK, foreign identifier, and withheld terminal checks passed on both venues.

| Cycle | Coin | Open fills (OKX; Bybit) | Actual open notional (OKX; Bybit) | Close fills (OKX; Bybit) | Terminal journal times (open; close, UTC) |
|---|---|---|---|---|---|
| 1 | 2Z short | 22 contracts @ 0.04476; 220 base @ 0.04483 | 9.84720; 9.86260 USDT | 22 contracts @ 0.04481; 220 base @ 0.04485 | 22:25:24.558; 22:25:35.627 |
| 2 | 2Z short | 22 contracts @ 0.04480; 220 base @ 0.04487 | 9.85600; 9.87140 USDT | 22 contracts @ 0.04480; 220 base @ 0.04483 | 22:25:37.054; 22:25:48.120 |
| 3 | LA short | 15 contracts @ 0.06729; 150 base @ 0.06740 | 10.09350; 10.11000 USDT | 15 contracts @ 0.06742; 150 base @ 0.06741 | 22:25:49.292; 22:26:00.357 |

The first report extraction missed the separate `step_chrono.jsonl` and exchange execution fields; the markers were recorded successfully. Step chronometry contains `ws_send` monotonic boundaries and `send_timing` rows with per-venue `owner_ws_send_started_ns`; captured venue frames contain Bybit `execTime` and OKX `fillTime`:

| Cycle | Phase | Signal → owner `ws.send` (Bybit; OKX, ms) | Signal → venue execution (Bybit `execTime`; OKX `fillTime`, ms) |
|---|---|---:|---:|
| 1 | Open | 2.01; 2.59 | 26; 30 |
| 1 | Close | 2.23; 2.95 | 21; 30 |
| 2 | Open | 3.12; 3.84 | 22; 31 |
| 2 | Close | 0.99; 1.60 | 20; 29 |
| 3 | Open | 2.13; 2.84 | 21; 31 |
| 3 | Close | 0.88; 1.42 | 20; 29 |

For send timing, the signal has a local wall-clock millisecond timestamp but no native monotonic timestamp. The estimate maps that signal through the same-process `step_chrono` wall/monotonic anchor and subtracts it from `owner_ws_send_started_ns`; millisecond quantization limits precision. Execution deltas use venue-provided `execTime` and `fillTime`, not local receipt time, against the local signal wall timestamp. Host/venue clock offset was not independently measured, so those deltas may include clock skew. The journal's approximately 1.06-second signal-to-terminal-report interval is local fill completion/receipt latency, not exchange execution latency. No profitability claim follows from these fills.

## 8. Recommended next step

Review the isolated code diff before any promotion beyond this authorized experiment. The live run and VPS-local artifacts do not establish mounted-storage durability or justify changing the active service configuration.
