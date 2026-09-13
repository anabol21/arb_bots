# Contour B fill-latency wave (2026-09-05)

## Purpose
Collect dual-leg fill-latency distribution on low-liquidity coins for the **model branch**, using Contour B send path (primitive dual `queue.put` → `ws.send`), not gear2 / W6 manager hot path.

## Contour (send path)
Identical to prior Contour B AB (`ws_ab_send_path` Contour B / historic `bybit_ws` shape):

1. Warm private+trade sockets (off hot path)
2. Prefetch signed dual-leg frames via `_b_live_w6_frames` (off hot path)
3. **Signal** stamp
4. `place_io_section` → `PrimitiveDualSender.enqueue_dual` → dual asyncio queues → long-lived sender `ws.send_text`
5. Parallel ack recv + fill observe (measurement)

**Not on hot path:** W6 recover / operator_approval / lease / journal prepare.

Orchestrator: VPS `validation/fill_latency_wave.py` (standalone; in-process symbol allowlist patch for SOPH/HMSTR/GALA; Contour B modules unchanged).

## Protocol (differs from AB B×5 trials)
- Coins (order): SOPH → HMSTR → GALA → TRUMP
- Cadence: 1 dual-leg action / minute
- Wave: open×4 then close×4 (8 min)
- Full: 8 waves = 64 actions / 64 minutes
- Smoke: 1 wave = 8 actions
- Size: matched dual targeting **$5.25** notional (buffer above Bybit $5 minNotional); **refresh size on each open** from live marks; close uses opened qty
- Per action: fresh bind+warm (unlike AB open→hold→close on one warm)

## Runs
| Run | Result | Notes |
|-----|--------|-------|
| Smoke 8m | 8/8 ok | OUT `.../fill_latency_smoke_8m/` |
| Full v1 | aborted ~#3 GALA | min-notional race (sized to ~$5.00, mark dipped) |
| Full v2 | **56/64 ok**, abort #57 | sizing fix held; GALA 7/7 opens |

Full v2 window (Minsk): 2026-09-05 17:00:41 → 17:56:53.  
Artifacts: `/data/bbot-gear2/private/ab_send_path/fill_latency_full_64m_v2/` (also box copy under reports).

## Latency summary (full v2, n=56 successful dual actions)

Overall p50 (ms):

| Metric | Bybit | OKX |
|--------|------:|----:|
| signal→send | 0.58 | 0.93 |
| ack RTT (send→recv) | 38.5 | 55.8 |
| fill_delivery | 37.2 | 31.5 |
| signal→venue_fill | 20.0 | 28.4 |

Per-coin signal→send p50 Bybit/OKX (ms): TRUMP 0.57/0.91, HMSTR 0.58/0.91, SOPH 0.57/0.96, GALA 0.61/0.91.

Rough planned notional sum (open+close): ~$306 Bybit + ~$306 OKX (not fee ledger).

## Abort #57 (SOPH open, wave 7)
- **Where:** `open_w6_production_bindings` → Bybit REST flat baseline **before** frame build / enqueue
- **Error:** `BaselineError: bybit position GET rejected` (`ws_w4_baseline._bybit_position_flat` when `retCode != 0`)
- **Meaning:** Bybit position REST returned non-zero retCode — **not** "position not flat", **not** Contour B send failure
- **Effect:** no order placed; no flatten needed; smoke coins ended flat
- **Likely:** transient Bybit REST / rate-limit / short API glitch after ~56 min of frequent baseline GETs (baseline does not surface retMsg in this raise)

## Comparability
`signal→send` / queue→send is comparable to 2026-09-04 Contour B AB (TRUMP B×5). Trial shape is not (staggered multi-coin vs open→hold→close).

## Out of scope / untouched
- gear2 systemd / strategy
- PR #20 deploy
- unmanaged SOL leftover short (still live during run; ignored)
