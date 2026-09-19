# Hot-add new coins (D0 discovery + D1 in-process spawn)

Track: collection/storage (D). Production entrypoint: `app/screaner_b_o.py`.

This patch adds a **default-off** path to subscribe new Bybit×OKX books without
restarting the live pool. It does **not** enable the flag on VPS, does **not**
restart `spread-collector`, and does **not** start a second collector on
`/data/live`.

Frozen (unchanged): websocket ingest (`okx_listener` / `bybit_listener` /
`_ws_listen_loop_v2`, subscribe payloads), exchange parsing (`handle_message`),
spread calculation (`calc_and_store_spread`), trading logic.

---

## D0 — discovery sidecar

Separate process. REST only. Never touches collector ingest.

```bash
python3 -m app.discovery \
  --universe /root/spread_staging/bybit_okx_universe.csv \
  --delta /root/spread_staging/hot_add_delta.csv \
  --max-new 8
```

Logic (from `screaner.ipynb`, not notebook cells):

1. Bybit `GET /v5/market/instruments-info?category=linear` (paginated).
2. OKX `GET /api/v5/public/instruments?instType=SWAP`.
3. Filters: OKX live USDT-settled SWAP; Bybit linear trading quote+settle USDT.
4. Inner join on `symbol_norm`.
5. Diff against **the universe CSV path you pass** (VPS staging CSV is 188
   `take=yes`; a local Desktop CSV may differ — do not mix them).
6. Atomic replace of the delta file. Hard cap `--max-new` /
   `SPREAD_DISCOVERY_MAX_NEW` (default 8).
7. **Does not rewrite** `take=yes` rows. The universe CSV is opened read-only.
   Delta path must not equal the universe path and must not sit under
   `/data/live`, `/data/bars`, `/data/compacted`, or `/data/spool`.

Delta columns (lot/tick kept for a later B1 bot seam):

`base_coin,okx_symbol,bybit_symbol,okx_tick_size,okx_lot_size,okx_min_size,bybit_tick_size,bybit_qty_step,bybit_min_order_qty,bybit_min_notional_value,discovered_at_utc`

No `take` column. This file is not the live pair screen.

---

## D1 — in-process hot-add

Env, **default OFF**:

| Env | Default | Meaning |
|-----|---------|---------|
| `SPREAD_HOT_ADD` | unset/off | Master switch. Off → live pool unchanged. |
| `SPREAD_HOT_ADD_DELTA` | `hot_add_delta.csv` | Delta snapshot from D0. Not REST. |
| `SPREAD_HOT_ADD_MAX_EXTRA` | `8` | Cap on coins beyond the import-time pool. |
| `SPREAD_HOT_ADD_POLL_SEC` | `30` | Poll interval. SIGHUP re-reads immediately. |
| `SPREAD_HOT_ADD_DROP` | `hot_add_drop.csv` | Drop snapshot (`base_coin` column). Gated by `SPREAD_HOT_ADD`. |

When on, the collector:

1. Inits `quotes[coin]` with the same shape as import-time.
2. `spawn_coin` starts the **existing** `okx_listener` / `bybit_listener`
   (connect scheduler already inside `_ws_listen_loop_v2`).
3. Replaces one-shot `asyncio.gather(*tasks)` with `TaskSupervisor` so tasks
   added after startup are awaited and cancelled on SIGTERM.

Missing delta while the flag is on: structured `hot_add_delta_missing`, keep
the original pool. Bad delta rows: `hot_add_row_invalid`, skip the row, do not
kill the process. Cap: `hot_add_cap_hit`, stop adding.

Heartbeat `pairs=` is `len(quotes)` so a successful hot-add is visible without
a restart.

### Drop API (`drop_coin`)

Orchestration only (no unsubscribe JSON; ingest frozen). Order matters:

1. `TaskSupervisor.cancel_named` for `okx:{coin}` and `bybit:{coin}` (and
   `okx-candle:` / `bybit-kline:` only when bar collection is on).
2. Short drain of those tasks.
3. `del quotes[coin]`.

Missing coin: `drop_coin_missing` error log, process continues. The extra cap
does not apply to drops. Drop file uses **snapshot** semantics (same as delta):
each mtime change applies the listed `base_coin` rows once.

---

## Canary unit (VPS experiments A–D)

Separate systemd unit — **not** `spread-collector.service`:

- Template: [`deploy/systemd/spread-collector-hotadd-canary.service`](../deploy/systemd/spread-collector-hotadd-canary.service)
- `SPREAD_HOT_ADD=1` **only** on this unit; production unit must stay off.
- Parquet: `/data/live-hotadd-canary`
- Spool: `/data/spool-hotadd-canary`
- Gaps: `/data/gaps-hotadd-canary`
- Log: `/var/log/spread/runtime-hotadd-canary.log`
- `InaccessiblePaths=/data/live` so the canary cannot publish into production
  ticks even if misconfigured.

Never run a second writer on `/data/live`. Do not restart production
`spread-collector` for these experiments. Do not fan-out `spread-bbot-theta-k1-canary`.

### Experiment runbook

| Stage | What | Driver / script |
|-------|------|-----------------|
| A | 2-coin bootstrap CSV (e.g. BTC+ETH), empty delta/drop, soak ticks | `validation/hot_add_canary_driver.py prepare` |
| B | Cumulative delta +1, then +3, then +13 (+10 window) — **one step per minute** | `validation/hot_add_canary_driver.py run` |
| C | Drop 1–2 **added** coins via `hot_add_drop.csv` | last step of `run` schedule |
| D | Full 188 `take=yes` canary vs prod parquet (read-only) | `validation/compare_hotadd_canary_live.py` |

Before stage B minute with +10 new coins, raise canary only:

```bash
# in spread-collector-hotadd-canary.service drop-in or systemctl edit:
Environment=SPREAD_HOT_ADD_MAX_EXTRA=16
sudo systemctl daemon-reload
sudo systemctl restart spread-collector-hotadd-canary
```

Production `SPREAD_HOT_ADD_MAX_EXTRA` stays at 8.

**VPS commands (after code deploy to `/root/spread_staging` on branch PR #53):**

```bash
sudo cp deploy/systemd/spread-collector-hotadd-canary.service /etc/systemd/system/
sudo systemctl daemon-reload
export STAGING=/root/spread_staging
python3 validation/hot_add_canary_driver.py prepare \
  --universe $STAGING/bybit_okx_universe.csv \
  --staging-dir $STAGING/canary-hotadd \
  --bootstrap BTC,ETH
# Override universe for canary only (drop-in):
# Environment=SPREAD_UNIVERSE=/root/spread_staging/canary-hotadd/canary_2coin.csv
# Environment=SPREAD_HOT_ADD_DELTA=/root/spread_staging/canary-hotadd/hot_add_delta.csv
# Environment=SPREAD_HOT_ADD_DROP=/root/spread_staging/canary-hotadd/hot_add_drop.csv
sudo systemctl enable --now spread-collector-hotadd-canary
python3 validation/hot_add_canary_driver.py run \
  --staging-dir $STAGING/canary-hotadd \
  --universe $STAGING/bybit_okx_universe.csv \
  --interval-sec 60
```

Watch canary log for `hot_add_spawned`, `hot_add_applied`, `hot_add_dropped`,
`ws_subscribe_ok`, `pairs=`. Production: `NRestarts=0`, `pairs=188`.

Experiment D go/no-go (defaults in compare script):

- Canary gap count in window ≤ production gap count.
- Canary p95 delivery proxy ≤ production p95 + **100 ms**.
- Per-coin tick count ratio (canary/prod) ≥ **0.95** on shared coins.

---

## Local proof (this patch)

```bash
python3 -m py_compile app/screaner_b_o.py app/utils/hot_add.py \
  app/utils/task_supervisor.py app/utils/universe_delta.py \
  app/discovery/intersection.py app/discovery/__main__.py
python3 -m unittest tests/test_universe_delta.py \
  tests/test_hot_add_supervisor.py tests/test_universe_discovery.py
python3 validation/check_hot_add.py
python3 -m py_compile validation/hot_add_canary_driver.py \
  validation/compare_hotadd_canary_live.py
# optional public REST into tmp (still no /data writes):
python3 validation/check_hot_add.py --live-rest
```

Local success is not VPS success. Optional `--live-rest` hits public Bybit/OKX
from this machine into a tmp delta; some cloud egress is CloudFront
geo-blocked (HTTP 403). That is an environment limit, not a collector
enable. Fixture tests cover intersection/diff/cap. Live REST belongs on
the VPS sidecar, still without turning `SPREAD_HOT_ADD` on.

---

## VPS enable gate — **not done in this patch**

Live facts already known (2026-09-18, read-only):

- `spread-collector` active, `pairs=188`, `NRestarts=0`.
- Only bot unit active: `spread-bbot-theta-k1-canary`. Do not fan-out that canary.
- Staging CSV `take=yes=188`. Discovery on VPS must diff
  `/root/spread_staging/bybit_okx_universe.csv`.

Do **not**:

- set `SPREAD_HOT_ADD=1` on the unit yet;
- restart `spread-collector` (that would resubscribe 188×2 sockets and open gaps);
- start a second `screaner_b_o.py` on `/data/live`;
- restart or retarget `spread-bbot-theta-k1-canary`.

Enable later, as its own change, only after this code is on the host **without**
turning the flag on, plus a dedicated observation window:

| Field | Value |
|-------|--------|
| Runtime | `app/screaner_b_o.py` via `spread-collector.service` |
| Log | `/var/log/spread/runtime.log` |
| First materialization | `/data/live` |
| Durable | `/data/live` + spool |
| Watch | accepted/published/spooled; NRestarts collector = 0; gaps only on **new** coins; heartbeat `pairs` rises by the spawned extra; canary bot NRestarts unchanged |

Until that window exists, production behavior with the flag off must stay
indistinguishable from the current pool.

---

## B1 bot seams (note only — no bot code in this patch)

Later bot hot-add is a **separate** contour. Do not implement it here.

| Seam | Rule |
|------|------|
| Meta | Same CSV + delta lot/tick columns. Fail-closed if lot/tick missing. |
| Sockets | Bot spawns **its own** WS in `app/bot/ws_books.py`. Do not share D sockets or read `/data/live`. |
| Cap | Hard cap. No auto-follow of every new listing. |
| Isolation | Do not write D parquet/spool/bars. Do not restart D. |
| Canary | Do **not** fan-out `spread-bbot-theta-k1-canary` onto new listings. |

Owner of that work: B Stub Runtime, after an explicit B1 task. Isolation
contract remains [`b-bot-isolation.md`](b-bot-isolation.md).
