# Local lean collector + production integration

Lean tick/bar contract for model-oriented ingest.

| Track | Entrypoint | Role |
|-------|------------|------|
| Parallel local smoke | [`app/screaner_local_lean.py`](../app/screaner_local_lean.py) | Naive local parquet; refuses `/data/live` |
| **Production (VPS)** | [`app/screaner_b_o.py`](../app/screaner_b_o.py) | Durable publisher/spool; lean behind flags |

Schema modules:

- Ticks lean body: [`app/schema/lean_event.py`](../app/schema/lean_event.py)
- Canary v1 body: [`app/schema/spread_event.py`](../app/schema/spread_event.py)
- Writer normalize: [`app/storage/writer.py`](../app/storage/writer.py) (`v1` / `lean` / `bar_5m`)

---

## Production flags (Option B)

Default **OFF** so code deploy does not change a running canary’s v1 output.

| Env | Default | Effect |
|-----|---------|--------|
| `SPREAD_LEAN_SCHEMA` | unset/off | `0` → v1 body (spread_*, freshness, `event_dt`, latencies). `1` → lean 16-col ticks, int64 ms stamps |
| `SPREAD_COLLECT_BARS` | unset/off | `1` → OKX business `candle5m` + durable bar publisher |
| `SPREAD_COLLECT_BYBIT_BARS` | unset/off | `1` → also Bybit `kline.5` (optional) |
| `SPREAD_BARS_ROOT` | `/data/bars` | Bar hive parent; files under `<root>/bar_5m/base_coin=…/event_date=…` |
| `SPREAD_BAR_PERSIST_EVERY` | `500` | Bar buffer flush threshold |

Tick root unchanged: `SPREAD_PARQUET_ROOT` (default `/data/live`).

### Enable for production accumulation

Do **not** flip these on a mid-run v1 process. Start a **new** process (systemd unit already carries lean flags — see [`docs/prod-unit-snippets.md`](prod-unit-snippets.md)):

```bash
export SPREAD_LEAN_SCHEMA=1
export SPREAD_COLLECT_BARS=1
export SPREAD_PARQUET_ROOT=/data/live
export SPREAD_BARS_ROOT=/data/bars
export SPREAD_SPOOL_ROOT=/data/spool
cd /root/spread_staging
# systemctl enable --now spread-collector.service
# or: /root/venv/bin/python app/screaner_b_o.py
```

Optional slice / soak overrides: `SPREAD_ROW_START`, `SPREAD_ROW_END`, `SPREAD_PERSIST_EVERY`, `SPREAD_BAR_PERSIST_EVERY`.

Compactor/backup continue to handle **existing v1** files under `/data/live`. Lean and v1 bodies should not be mixed in the same day partition without a reader that dual-reads.

---

## How to run (local parallel script)

From repo root:

```bash
# Smoke: 2 coins, flush every 50 rows
python3 app/screaner_local_lean.py --row-end 2 --persist-every 50

# Full universe slice (default rows 0:337), ticks + OKX 5m bars
python3 app/screaner_local_lean.py

# Ticks only
python3 app/screaner_local_lean.py --no-bars

# Also collect Bybit 5m klines (optional; model canon is OKX)
python3 app/screaner_local_lean.py --bybit-bars
```

Env overrides (local script only):

| Env | Default |
|-----|---------|
| `SPREAD_LEAN_PARQUET_ROOT` | `output/lean_ticks` |
| `SPREAD_LEAN_BARS_ROOT` | `output/lean_bars` |
| `SPREAD_LEAN_RUNTIME_LOG` | `output/lean_runtime.log` |
| `SPREAD_LEAN_UNIVERSE` | `bybit_okx_universe.csv` |

Layout (local script):

```text
output/lean_ticks/base_coin=<COIN>/event_date=<YYYY-MM-DD>/batch_*.parquet
output/lean_bars/bar_5m/base_coin=<COIN>/event_date=<YYYY-MM-DD>/batch_*.parquet
```

The local script refuses to write under `/data/live`, `/data/spool`, or `/mnt/storage`.

---

## Lean tick columns written

No `spread_*`, `max_*`, freshness, `event_dt`, latency:

| Column |
|--------|
| `event_local_ts_ms` |
| `base_coin` |
| `trigger` |
| `calc_local_ts_ms` |
| `okx_local_recv_ts_ms` |
| `okx_ts_ms` |
| `bybit_local_recv_ts_ms` |
| `bybit_ts_ms` |
| `okx_bid_price` / `okx_bid_size` / `okx_ask_price` / `okx_ask_size` |
| `bybit_bid_price` / `bybit_bid_size` / `bybit_ask_price` / `bybit_ask_size` |

Derive at read: `spread_long` / `spread_short`, latency, freshness, `event_dt`.

Tick L1 channels:

- OKX public WS `books5` — `wss://ws.okx.com:8443/ws/v5/public`
- Bybit linear `orderbook.1.{symbol}` — `wss://stream.bybit.com/v5/public/linear`

---

## 5m bar channels

### Primary (model `ref_exchange=okx`)

| Item | Choice |
|------|--------|
| Endpoint | `wss://ws.okx.com:8443/ws/v5/business` (**business**, not public) |
| Channel | `candle5m` with `instId` = OKX SWAP id |
| Persist when | `confirm == "1"` (closed candle only) |
| `volume` field | **`volCcy`** — SWAP trading volume in **base currency** |

### Optional (Bybit, off by default)

| Item | Choice |
|------|--------|
| Topic | `kline.5.{symbol}` |
| Persist when | `confirm == true` |
| `volume` | linear USDT **base coin** |

### Bar parquet columns

`bar_start_ts_ms`, `bar_end_ts_ms` (= start + 300_000), `base_coin`, `ref_exchange`, `volume`

No OHLC, no unit metadata columns.

---

## Units note (do not put in parquet)

Lot / tick / min-size metadata lives in [`bybit_okx_universe.csv`](../bybit_okx_universe.csv). Join at analysis by `base_coin`.

---

## Canary safety

| Action | Safe? |
|--------|-------|
| Deploy code to `/root/spread_staging` with flags unset | Yes — running process keeps old code in memory; new launches default v1 |
| Set `SPREAD_LEAN_SCHEMA=1` on live canary mid-run | **No** |
| Kill/restart canary to pick up lean | Only after canary accounting / explicit approval |
