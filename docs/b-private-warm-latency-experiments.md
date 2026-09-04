# B-private — Warm-Lat experiments (warm private WS trade latency)

Track: glue / B-private. Code: `app/bot/private/ws_warm_latency.py`,
`app/bot/private/warm_latency_stages.py`. Status row: **Warm-Lat** in
[`b-private-status.md`](b-private-status.md).

**Goal.** After process-lifetime private+trade WS is already `ready=True`,
measure remaining place-path delay with tight stage timestamps. Isolate
framework overhead (approval / lease / profile / prepare) from venue RTT.

This package **measures**; it does not rewrite the production hot-path
(prefetch / parallel place belong to another agent). **Agents must not SSH
or run live send** — humans only, after this PR is in `dev`.

Print the exact VPS recipe anytime:

```bash
python -m app.bot.private --ws-warm-latency --warm-lat-print-vps-recipe
```

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

## 4. Coin / notional profile (live)

**Use the immutable W6/W7 TRUMP profile** (same risk discipline as prior live dual-leg):

| Leg | Symbol | Side (open) | Qty | ≈ notional |
|-----|--------|-------------|-----|------------|
| Bybit | `TRUMPUSDT` | buy | `4.0` | ~$6–8 |
| OKX | `TRUMP-USDT-SWAP` | sell | `40` contracts (ctVal 0.1) | ~$6–8 |

- Clears Bybit `minNotionalValue` ≈ 5 USDT; lots stay matched across venues.
- Cap ≪ **100 USD/venue**; `K_live=1`.
- **Do not substitute SOL/XRP gear2 sizes** here — the private dual-leg harness
  rejects non-TRUMP plans. SOL/XRP remain public/stub sizing, not this send path.

---

## 5. Env flags / CLI (local dry)

```bash
python -m app.bot.private --ws-warm-latency \
  --warm-lat-n=20 \
  --warm-lat-path=AB \
  --warm-lat-mode=serial \
  --warm-lat-send=false \
  --warm-lat-out=/tmp/warm_lat
```

| Flag | Values | Default / note |
|------|--------|----------------|
| `--warm-lat-n=` | dry 1..50; **live 1..10** | required; live start with **1**, then ≤5 |
| `--warm-lat-path=` | `A` \| `B` \| `AB` | `AB` (live prefer `B`) |
| `--warm-lat-mode=` | `serial` \| `parallel` \| `single` | `serial` (live prefer `parallel` to match W7) |
| `--warm-lat-venue=` | `dual` \| `bybit` \| `okx` | `dual` |
| `--warm-lat-send=` | `false` \| `true` | `false` |
| `--warm-lat-out=` | directory | data-root `/warm_lat` |
| `--warm-lat-approve-one-shot` | flag | **required** for live |
| `--warm-lat-print-vps-recipe` | flag | print VPS recipe and exit 0 |

Live gates (same pattern as W6/W7): `VENUE=live` + `LIVE_ORDERS=1` +
`BBOT_PRIVATE_WARM_LAT=1` + `--warm-lat-approve-one-shot`. Default CLI never
opens sockets; `LIVE_ORDERS=1` alone never sends.

---

## 6. VPS live recipe (exact)

**Environment claim:** VPS runtime on `root@38.180.94.108`, code
`/root/spread_staging`, results under `/data/bbot-gear2/private/warm_lat/`.
This is **not** local proof of production correctness.

### 6.1 Preflight (read-only)

```bash
ssh root@38.180.94.108 'systemctl is-active spread-collector; systemctl show -p MainPID --value spread-collector'
```

- Expect `active` and a stable MainPID.
- **Do not** restart `spread-collector`, D compactor, or D backup timers.

### 6.2 Surgical deploy (files only)

Copy **only** these paths into staging (preserve VPS-only flatten extras /
aplace / sample-cap close-skip patches):

```text
app/bot/private/warm_latency_stages.py
app/bot/private/ws_warm_latency.py
app/bot/private/ws_gates.py
app/bot/private/harness_readonly.py
```

From a machine that has the PR tree (repo root):

```bash
HOST=root@38.180.94.108
STAGING=/root/spread_staging
for f in \
  app/bot/private/warm_latency_stages.py \
  app/bot/private/ws_warm_latency.py \
  app/bot/private/ws_gates.py \
  app/bot/private/harness_readonly.py
do
  scp "$f" "$HOST:$STAGING/$f"
done
```

**Do not:**

- `git reset --hard` / full-repo `rsync` / overlay entire checkout over
  `/root/spread_staging`
- Replace collector trees, systemd units, or VPS-only patches (flatten /
  aplace / sample-cap close skip)
- Deploy secrets; live env stays `/etc/spread/bbot-private-live.env` (mode 600)

Optional: also copy `docs/b-private-warm-latency-experiments.md` for on-box
reference (docs-only; not required to run).

### 6.3 Live command (Path B on warm session)

```bash
ssh root@38.180.94.108
set -a
source /etc/spread/bbot-private-live.env   # mode 600; not git
set +a
export VENUE=live
export LIVE_ORDERS=1
export BBOT_PRIVATE_WARM_LAT=1
export BBOT_PRIVATE_DATA_ROOT=/data/bbot-gear2/private
mkdir -p /data/bbot-gear2/private/warm_lat

cd /root/spread_staging
/root/venv/bin/python -m app.bot.private --ws-warm-latency \
  --warm-lat-n=1 \
  --warm-lat-path=B \
  --warm-lat-mode=parallel \
  --warm-lat-venue=dual \
  --warm-lat-send=true \
  --warm-lat-approve-one-shot \
  --warm-lat-out=/data/bbot-gear2/private/warm_lat
```

After `n=1` is `status=ok` and `notes.w6_flat_after=true`, optionally repeat
with `--warm-lat-n=5` (hard cap live `n≤10`). Prefer `parallel` (W7 shape) for
the speed reference; use `serial` only if comparing to W6.

Journal + results isolation:

| Path | Role |
|------|------|
| `/data/bbot-gear2/private/` | `BBOT_PRIVATE_DATA_ROOT` for this experiment |
| `/data/bbot-gear2/private/warm_lat/` | JSON/CSV results |
| `/data/bbot/private/` | Historic B-private adapter journal (leave alone unless intended) |
| `/data/live`, `/data/bars`, `/data/compacted`, `/data/spool` | **Forbidden** |

---

## 7. Output schema (parse after run)

`schema_version`: `warm_lat_results.v1`

Files under `--warm-lat-out`:

| File | Contents |
|------|----------|
| `warm_lat_results.json` | Full report (stdout mirrors this) |
| `warm_lat_cycles.csv` | Long-form: one row per cycle × interval |
| `warm_lat_summary.csv` | One row per bucket × interval with p50/p95 |

### 7.1 Top-level JSON keys

| Key | Use |
|-----|-----|
| `schema_version` | `warm_lat_results.v1` |
| `status` | `ok` required before trusting latency |
| `warm_ready` | must be `true` for live |
| `summary` | **p50/p95 here** |
| `path_ab_delta_ms` | A vs B on shared intervals (dry/AB) |
| `notes.w6_status` / `notes.w6_flat_after` / `notes.safety_ok` | live safety gate |
| `notes.output_paths` | absolute paths written |
| `how_to_read_summary` | machine hint for parsers |
| `trade_lat_model_ms` | `100` (model assumption; do not rewrite gear 1.0 from one sample) |

### 7.2 `summary` bucket shape

Bucket key:

```text
path_{A|B}|venue_{bybit|okx}|mode_{serial|parallel|single}
```

Per interval object:

```json
{ "n": 5, "mean": 12.3, "p50": 11.0, "p95": 20.1, "min": 9.0, "max": 22.0 }
```

Units: **milliseconds**.

Primary live intervals to report:

- `order_prepared_to_request_sent` (local_prepare analogue)
- `request_sent_to_ack` (venue RTT; not one-way)
- `ack_to_terminal`
- `warm_ready_to_terminal`

### 7.3 One-liner parse

```bash
python3 - <<'PY'
import json
from pathlib import Path
p = Path("/data/bbot-gear2/private/warm_lat/warm_lat_results.json")
d = json.loads(p.read_text())
assert d["schema_version"] == "warm_lat_results.v1"
print("status", d["status"], "safety_ok", d.get("notes", {}).get("safety_ok"))
print("flat_after", d.get("notes", {}).get("w6_flat_after"))
for bucket, intervals in sorted(d.get("summary", {}).items()):
    if not bucket.startswith("path_B"):
        continue
    for name in (
        "order_prepared_to_request_sent",
        "request_sent_to_ack",
        "ack_to_terminal",
        "warm_ready_to_terminal",
    ):
        s = intervals.get(name) or {}
        if s.get("n"):
            print(f"{bucket} {name}: p50={s['p50']:.3f} p95={s['p95']:.3f} n={s['n']}")
PY
```

Or use `warm_lat_summary.csv` columns: `bucket,interval,n,mean,p50,p95,min,max`.

---

## 8. Abort / safety (half-leg)

Live Path B reuses W6/W7 place+flatten on the warm session:

| Expectation | Detail |
|-------------|--------|
| `notes.w6_status` | `ok` |
| `notes.w6_flat_after` | **`true`** before trusting any latency numbers |
| `notes.safety_ok` | `true` iff status ok **and** flat_after |
| Max N | live hard cap **10**; start **1**, then ≤5 |
| On abort / one-leg leftover | W6 flattens **both** venues, increments `n_aborted`, **stops** further n |
| If `flat_after!=true` or `status!=ok` | **STOP**. Do not raise n. Verify flat on both exchanges (UI / baseline). Resolve leftover before any retry |
| Process exit | non-zero when live safety fails |

Do **not** leave exposure to “finish the latency sample later.”

---

## 9. What NOT to do

- Do **not** restart `spread-collector`, D compactor, or D backup timers.
- Do **not** overlay full git over VPS flatten/aplace/sample-cap staging trees.
- Do **not** write `/data/live`, `/data/bars`, `/data/compacted`, `/data/spool`.
- Do **not** put keys in git, chat, or collector logs.
- Do **not** run live send from an agent; human one-shot only.
- Do **not** change gear-1.0 `Trade_Lat` from a short Warm-Lat sample.
- Do **not** treat Path A dry stub ack as venue physics.
- Do **not** swap in SOL/XRP sizes on this harness without a separate profile unlock.

---

## 10. Success metrics vs `Trade_Lat=100ms`

| Metric | Use |
|--------|-----|
| Path B `order_prepared→request_sent` | Framework/journal local_prepare on warm session |
| Path B `request_sent→ack` / `ack→terminal` | Venue + transport after warm |
| Path A `intent→request_sent` (dry) | Minimal queue→send floor |
| `path_ab_delta_ms` | Framework vs minimal send |
| vs `Trade_Lat=100ms` | Honesty check for model; one sample ≠ change baseline |

---

## 11. Tests

Hermetic (no live network):

```bash
python3 -m unittest tests.test_warm_latency_stages -v
```

Covers stage labels, interval math, p50/p95, JSON/CSV + `schema_version`,
dry A/B, live gate refusals, live N cap, VPS recipe printer.
