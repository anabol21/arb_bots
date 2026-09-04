# B-private — Warm-Lat experiments (warm private WS trade latency)

Track: glue / B-private. Code: `app/bot/private/ws_warm_latency.py`,
`app/bot/private/warm_latency_stages.py`. Status row: **Warm-Lat** in
[`b-private-status.md`](b-private-status.md).

**Goal.** After process-lifetime private+trade WS is already `ready=True`,
measure remaining place-path delay with tight stage timestamps. Isolate
framework overhead (approval / lease / profile / prepare) from venue RTT.

This package **measures**; it does not rewrite the production hot-path
(prefetch / parallel place belong to another agent).

---

## 1. Why (context)

Public L1 stays process-lifetime. Warm private supervisor
(`ws_warm_session.py`) does the same for private+trade. After warm, live gear2
still sees ~5–6 s signal→done in some runs; venue ACK is much smaller
(Bybit ~0.9–1.2 s / OKX ~0.1 s in prior notes) while `local_prepare`
(~0.7–0.9 s/leg) often dominates.

Legacy speed reference: gear-1 `else/bybit_ws.py` (process-lifetime
public+private WS; order = JSON → `asyncio.Queue` → long-lived sender
`ws.send` — no lease/approval/journal on the critical path).

**Note:** `else/bybit_ws.py` is **not** on this branch. Path A implements that
queue→send *shape* with stubs; it does not copy secrets/config from the
legacy file.

Model assumption to compare against: `Trade_Lat` = **100 ms**
([`gear-2-private-params.md`](gear-2-private-params.md)). Warm-Lat reports
p50/p95 per stage; do **not** change `Trade_Lat` from this experiment alone.

---

## 2. Stage labels

Monotonic marks (ns), then intervals in ms:

| Stage | Meaning |
|-------|---------|
| `warm_ready` | Session `is_ready()` (or dry equivalent) |
| `intent` | Place intent starts |
| `approval` | Operator approval issue/consume region |
| `lease` | Lease supervisor `assert_can_send` |
| `profile` | Metadata + position-mode preflight |
| `order_prepared` | Journal `order_prepared` durable |
| `request_sent` | Would-send / actual WS send |
| `ack` | ACK received (or dry stub) |
| `terminal` | Fill/cancel terminal (or dry stub) |

Path A **skips** approval/lease/profile/order_prepared on the critical path
(marks them skipped). Path B dry stamps each framework stage and stops at
**would-send** (`request_sent`/`ack`/`terminal` skipped). Path B live derives
prepare/send/ack/terminal from the private journal after W6/W7 on the warm
session (discrete approval/lease/profile collapsed when not separately
stamped).

---

## 3. A/B paths

| Path | Shape | Dry | Live |
|------|-------|-----|------|
| **A** | Pre-built JSON → queue → long-lived `ws.send` | Fake warm socket | **Unsupported** (do not bypass venue protocol) |
| **B** | Production prepare (approval→lease→profile→prepare→send) | Real journal/vault/lease/preflight; `dispatch_transport=False` | Gated; reuses W6 serial or W7 parallel on warm session |
| **AB** | Both | Default for local CI | Live runs A dry + B live in one process |

---

## 4. Env flags / CLI

Dry (default, no network, safe for CI):

```bash
python -m app.bot.private --ws-warm-latency \
  --warm-lat-n=20 \
  --warm-lat-path=AB \
  --warm-lat-mode=serial \
  --warm-lat-send=false \
  --warm-lat-out=/tmp/warm_lat
```

Live send (human on VPS only — **agents must not run this**):

```bash
# source /etc/spread/bbot-private-live.env  (mode 600, not git)
export VENUE=live LIVE_ORDERS=1 BBOT_PRIVATE_WARM_LAT=1
python -m app.bot.private --ws-warm-latency \
  --warm-lat-n=5 \
  --warm-lat-path=B \
  --warm-lat-mode=parallel \
  --warm-lat-send=true \
  --warm-lat-approve-one-shot \
  --warm-lat-out=/data/bbot/private/warm_lat
```

Same pattern as W6/W7: `VENUE=live` + `LIVE_ORDERS=1` + experiment opt-in +
`--warm-lat-approve-one-shot`. Default CLI never opens sockets; `LIVE_ORDERS=1`
alone never sends.

Other flags:

| Flag | Values | Default |
|------|--------|---------|
| `--warm-lat-n=` | 1..50 | required |
| `--warm-lat-path=` | `A` \| `B` \| `AB` | `AB` |
| `--warm-lat-mode=` | `serial` \| `parallel` \| `single` | `serial` |
| `--warm-lat-venue=` | `dual` \| `bybit` \| `okx` | `dual` |
| `--warm-lat-send=` | `false` \| `true` | `false` |
| `--warm-lat-out=` | directory | data root `/warm_lat` |

Outputs: `warm_lat_results.json`, `warm_lat_cycles.csv`, `warm_lat_summary.csv`
(p50/p95 per stage, per venue, serial vs parallel, Path A vs B delta).

---

## 5. How to run on VPS later

Environment claims: **VPS runtime** + journal under `/data/bbot/private/` —
not local proof of production correctness.

1. Deploy only the private bot tree / staged checkout under
   `/root/spread_staging` (or the current B-private staging path). **Do not**
   overlay a full git checkout over VPS flatten/aplace layout.
2. Confirm `spread-collector` stays `active`. **Do not** restart the collector
   for this experiment.
3. Confirm warm private session is the production supervisor
   (`ws_warm_session.py`); Warm-Lat live path starts/attaches the same
   process-lifetime warm session before place cycles.
4. Use matched TRUMP profiles (~$6–8/leg, ≪ 100 USD/venue), `K_live=1`, small
   `--warm-lat-n`.
5. Write results under `/data/bbot/private/warm_lat/` (never `/data/live`,
   `/data/bars`, `/data/compacted`).
6. Compare p50 `request_sent→ack` and `warm_ready→terminal` to
   `Trade_Lat=100ms`. Treat `local_prepare` /
   `order_prepared→request_sent` as framework cost vs Path A.

---

## 6. What NOT to do

- Do **not** restart `spread-collector`, D compactor, or D backup timers.
- Do **not** overlay full git over VPS flatten/aplace production trees.
- Do **not** put keys in git, chat, or collector logs.
- Do **not** run live send from an agent; human one-shot only.
- Do **not** change gear-1.0 `Trade_Lat` from a short Warm-Lat sample.
- Do **not** treat Path A dry stub ack as venue physics.
- Do **not** write into D trees (`/data/live`, `/data/bars`, `/data/compacted`).

---

## 7. Success metrics

| Metric | Use |
|--------|-----|
| Path B `intent→order_prepared` / discrete approval+lease+profile | Framework overhead on warm session |
| Path B `order_prepared→request_sent` | Journal/prepare → send (local_prepare analogue) |
| Path B `request_sent→ack` / `ack→terminal` | Venue + transport after warm |
| Path A `intent→request_sent` | Minimal queue→send floor |
| `path_ab_delta_ms` | How much of delay is framework vs minimal send |
| vs `Trade_Lat=100ms` | Honesty check for model; one sample ≠ change baseline |

---

## 8. Tests

Hermetic (no live network):

```bash
python -m unittest tests.test_warm_latency_stages -v
```

Covers stage labels, interval math, p50/p95, JSON/CSV writers, dry A/B CLI
gate refusals for live without opt-in/approve.
