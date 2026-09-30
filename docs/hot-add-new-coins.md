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
5. Crypto-yes gate (`research.is_crypto.is_hot_add_crypto`): skip denylist
   names; skip Bybit `symbolType` in `stock` / `forex` / `commodity` /
   `xstocks` (HUT/TEAM/TEM-style stock perps). Empty `symbolType` is Bybit's
   crypto-linear default. Unknown non-empty types are **not** added.
   `is_crypto` itself still defaults unknown names to True — discovery must
   not use that default.
6. Diff against **the universe CSV path you pass** (VPS staging CSV is 188
   `take=yes`; a local Desktop CSV may differ — do not mix them).
7. Atomic replace of the delta file. Hard cap `--max-new` /
   `SPREAD_DISCOVERY_MAX_NEW` (default 8).
8. **Does not rewrite** `take=yes` rows. The universe CSV is opened read-only.
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

## Canary unit (VPS experiments A–D and thin listing-wait)

Separate systemd unit — **not** `spread-collector.service`:

- Template: [`deploy/systemd/spread-collector-hotadd-canary.service`](../deploy/systemd/spread-collector-hotadd-canary.service)
- Discovery timer (listing-wait): [`spread-discovery-hotadd-canary.timer`](../deploy/systemd/spread-discovery-hotadd-canary.timer)
- `SPREAD_HOT_ADD=1` **only** on this unit; production unit must stay off.
- Parquet: `/data/live-hotadd-canary`
- Spool: `/data/spool-hotadd-canary`
- Gaps: `/data/gaps-hotadd-canary`
- Log: `/var/log/spread/runtime-hotadd-canary.log`
- `InaccessiblePaths=/data/live` so the canary cannot publish into production
  ticks even if misconfigured.
- Thin canary clone: `/root/spread_hotadd_canary` (not `/root/spread_staging`).
- Universe: `/root/spread_hotadd_canary/bybit_okx_universe_canary10.csv` — **full**
  prod copy + REST backfill (`take=no` for missing intersection rows), then
  `take=yes` on exactly **10 crypto** pairs (`research/is_crypto.py`). Discovery
  requires **crypto-yes** (`is_hot_add_crypto`: denylist pass **and** Bybit
  `symbolType` not TradFi). Hot-add still skips denylist `is_crypto=no` names.

Never run a second writer on `/data/live`. Do not restart production
`spread-collector` for these experiments. Do not fan-out `spread-bbot-theta-k1-canary`.

### Thin listing-wait canary (10 pairs)

Goal: wait for a **real** new Bybit×OKX listing while production stays at
188 pairs. Do **not** use a 10-row CSV slice — that makes discovery fill
`MAX_EXTRA` immediately.

| Mode | Signals |
|------|---------|
| **Idle** | Heartbeat `pairs=10`; `hot_add_applied` empty; discovery JSON each cycle: large `csv_coins`, `delta_rows=0`, `new_before_cap=0`; prod `pairs=188`, `NRestarts=0` |
| **Event** | Delta row for `base_coin` absent from canary copy **and** prod CSV; `hot_add_spawned` + `ws_subscribe_ok`; `pairs=11` (or +k ≤ 8); starter 10 still tick |
| **Abort** | First hour `pairs` > 10 without a coin missing from the full copy; delta coin already in copy or prod CSV; any write under `/data/live`; prod collector restart |

Prep (once per canary start):

```bash
export CANARY_ROOT=/root/spread_hotadd_canary
export PROD_CSV=/root/spread_staging/bybit_okx_universe.csv
cd $CANARY_ROOT   # branch cursor/hot-add-new-coins-58c9 (PR #53)
python3 validation/prep_canary10_universe.py \
  --prod-universe "$PROD_CSV" \
  --out "$CANARY_ROOT/bybit_okx_universe_canary10.csv" \
  --dry-run-discovery \
  --delta "$CANARY_ROOT/hot_add_delta.csv"
# Log line canary10_written lists the 10 take=yes names — keep for compare.
```

Start (does **not** restart `spread-collector`):

```bash
sudo cp deploy/systemd/spread-collector-hotadd-canary.service /etc/systemd/system/
sudo cp deploy/systemd/spread-discovery-hotadd-canary.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo mkdir -p /data/live-hotadd-canary /data/spool-hotadd-canary /data/gaps-hotadd-canary
sudo systemctl enable --now spread-collector-hotadd-canary
sudo systemctl enable --now spread-discovery-hotadd-canary.timer
```

Run window: **7 days** or first listing event (whichever comes first). After a
successful hot-add, optional drop of **that new coin only** via
`hot_add_drop.csv` (snapshot) to prove the starter 10 survive — do not drop the
starter 10 in this experiment.

Discovery logs (journal): `discovery_done | ... | csv_coins=... | delta_rows=... | coins=...`

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
  tests/test_hot_add_supervisor.py tests/test_universe_discovery.py \
  tests/test_canary10_universe.py
# crypto-yes gate: HUT/TEAM/TEM stock symbolType out of delta; BTC in
python3 -m py_compile validation/prep_canary10_universe.py \
  app/utils/canary10_universe.py app/utils/canary10_guards.py
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

## B1 bot hot-add (`BBOT_HOT_ADD`)

Separate contour from D1. Bot process uses **only** `BBOT_*` env names — never
`SPREAD_HOT_ADD_*`. Code: `app/bot/hot_add.py` + `BotRuntime.spawn_coin` /
`drop_coin` in `app/bot/runtime.py`. Poller/delta helpers are shared
(`run_hot_add_poller`, `universe_delta`); collector env helpers are not.

| Env | Default | Meaning |
|-----|---------|---------|
| `BBOT_HOT_ADD` | unset/off | Master switch. Off → `asyncio.gather` pool unchanged. |
| `BBOT_HOT_ADD_DELTA` | `{data_root}/hot_add_delta.csv` | Relative paths resolve under `BBOT_DATA_ROOT`. |
| `BBOT_HOT_ADD_DROP` | `{data_root}/hot_add_drop.csv` | Drop snapshot (`base_coin`). Same mtime semantics as D. |
| `BBOT_HOT_ADD_MAX_EXTRA` | `8` | Cap on coins beyond the import-time `BBOT_COINS` pool. |
| `BBOT_HOT_ADD_POLL_SEC` | `30` | Poll interval. SIGHUP re-reads when flag is on. |

| Seam | Rule |
|------|------|
| Meta | Same delta columns as D0. Fail-closed if coin cannot resolve positive lot/tick (universe CSV hit with bad meta, or absent from universe **and** delta lot/tick incomplete). |
| Sockets | Spawns `run_okx_books5` / `run_bybit_orderbook1` via `TaskSupervisor`. No D sockets, no `/data/live`. |
| Cap | `BBOT_HOT_ADD_MAX_EXTRA` (bot-isolated). |
| Isolation | Writes only under `BBOT_DATA_ROOT`. Do not restart D. Do not enable on theta-k1 canary by default. |
| Canary | Template only — do **not** fan-out `spread-bbot-theta-k1-canary`. Optional dedicated unit later; not deployed in this patch. |

When off, bot behavior is indistinguishable from the pre-B1 gather path.

Local proof:

```bash
python3 -m py_compile app/bot/hot_add.py app/bot/hot_add_warm.py app/bot/runtime.py
python3 -m unittest tests/test_bbot_hot_add.py tests/test_bbot_hot_add_warm.py
```

Isolation contract remains [`b-bot-isolation.md`](b-bot-isolation.md).

---

## B2 bot hot-add → would_send warm (`BBOT_HOT_ADD` + history)

Extends B1 so a hot-added coin can enter the **gear 2.2 would_send** path
(`policy.decide` via theta K=1) without waiting ~12h of live SMA-12. Still
**would_send only** (`send=false`). Do **not** fan-out or restart
`spread-bbot-theta-k1-canary` — run a **duplicate canary** unit with its own
`BBOT_DATA_ROOT`.

Code: `app/bot/hot_add_warm.py` + `BotRuntime._warm_hot_added_coin` /
`_trade_coin_order` / `drop_coin` abort in `app/bot/runtime.py`.

| Env | Default | Meaning |
|-----|---------|---------|
| `BBOT_HOT_ADD_WARM` | on (when unset) | Set `0` to skip history warm (coin stays non-eligible for trades). |
| `BBOT_HOT_ADD_HISTORY_ROOT` | unset → `{BBOT_DATA_ROOT}` | Read-only history root. Relative paths resolve under `BBOT_DATA_ROOT`. |
| `BBOT_HOT_ADD_HISTORY_HOURS` | `12` | Minimum history window for slim-tick span check. |
| `BBOT_HOT_ADD_HISTORY_MIN_SMA12` | `40` | Min finite SMA-12 tips per side after warm (fail-closed if short). |

| Seam | Rule |
|------|------|
| Warm source 1 | `{history_root}/floor/event_date=*/metrics.jsonl` via existing `build_warm_state_from_floor_journal` (same payload shape as restart `floor_warm.pkl`). |
| Warm source 2 | Slim ticks: `{history_root}/spread_ticks.jsonl` (or `spreads/*.jsonl|*.csv`) with `event_local_ts_ms,base_coin,spread_long,spread_short`. Replay through `LiveFloorObserver.note_spreads`. |
| Parquet | **Not** read in-process (no pandas/pyarrow on the bot path). Offline: materialize floor journal or slim ticks under `HISTORY_ROOT`. |
| Eligibility | Bootstrap `BBOT_COINS` start eligible. Hot-added coins join `_trade_eligible` only after warm OK. `_run_theta_trade` passes `_trade_coin_order()` into `policy.decide`. |
| Fail-closed | Missing/short history → log `bbot_hot_add_warm_fail_closed`; keep WS+quotes for observability; **do not** open would_send on that coin. |
| Drop | `abort_coin_if_held` clears K=1 if needed; drop floor/TW-p50 state; cancel WS; shrink maps. No orphan observers. |
| Cap | Unchanged: `BBOT_HOT_ADD_MAX_EXTRA` (default 8). |

Local proof:

```bash
python3 -m py_compile app/bot/hot_add.py app/bot/hot_add_warm.py app/bot/runtime.py
python3 -m unittest tests/test_bbot_hot_add.py tests/test_bbot_hot_add_warm.py
```

### Duplicate would_send canary checklist (ops — do not deploy from this patch)

1. **Own tree**: clone unit e.g. `spread-bbot-theta-k1-hotadd-canary` — never edit the live theta-k1 unit.
2. **Own data root**: `BBOT_DATA_ROOT=/data/bbot-theta-k1-hotadd-canary` (writable journals only here).
3. **History RO mount**: prepare a readable copy of collector history (floor journal and/or slim ticks). Example: offline oneshot from compacted → floor metrics under `/data/bbot-hotadd-history`, then:
   - `BBOT_HOT_ADD_HISTORY_ROOT=/data/bbot-hotadd-history`
   - systemd `ReadOnlyPaths=/data/bbot-hotadd-history` (and optionally RO compact copy). Prefer **not** granting live unit write to `/data/compacted` / `/data/live`.
4. **Flags**: `BBOT_HOT_ADD=1`, `BBOT_HOT_ADD_WARM=1`, `BBOT_PROFILE=gear22_would_send`, `BBOT_BROKER=stub`, theta/floor/tw_p50 watches on (profile defaults).
5. **Driver**: reuse D/B1 delta/drop files under the canary data root (`hot_add_delta.csv` / `hot_add_drop.csv`) with full lot/tick columns.
6. **Green**:
   - `bbot_hot_add_spawned | … | trade_eligible=true`
   - `bbot_hot_add_warm_ok | source=floor_journal` (or `slim_spreads`)
   - theta / `theta_trades` rows can cite the hot-added coin
   - drop → `bbot_hot_add_dropped`, no leftover `okx:{coin}` / `bybit:{coin}` tasks; slot cleared if held
   - missing history coin → `trade_eligible=false` + `bbot_hot_add_warm_fail_closed`, bootstrap coins still trade
7. **Abort**: any write under `/data/live`; NRestarts on production collector or live theta-k1 canary; live `send=true`.

Isolation contract remains [`b-bot-isolation.md`](b-bot-isolation.md).
Would_send contour: [`gear22-theta-k1-would-send.md`](gear22-theta-k1-would-send.md).

