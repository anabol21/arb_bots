# HL v2 canary (sharded l2Book)

> **Integration branch: `main_hl`.** Hyperliquid / HL scaling work lives on long-lived `main_hl` only. Open HL PRs against `main_hl`. Do **not** merge HL work into `main` without an explicit decision. Prod-next on `main` stays Bybit/OKX → `/data/live`.

Isolated Track-1 experiment. **Not** prod-next. **Do not** write `/data/live`.

## What it is

One process (`python -m app.hl_v2`) that:

1. Loads `take=yes` Bybit∩OKX pairs from a universe CSV.
2. Exact-intersects with Hyperliquid perp `meta` (~88 coins typical).
3. Subscribes Bybit `orderbook.1` + OKX `books5` in the same staff style as prod-next lean (same columns / ts fields / parquet rotation via `ParquetPublisher`).
4. Subscribes Hyperliquid **`l2Book` only** (no trades), maps top-of-book → `hl_bid/ask_price/size`.
5. Shards HL coins across `HL_SOCKET_COUNT` websockets (default **1**, round-robin; raise to 2–9 later without rewrite).
6. Writes lean+HL rows (no spread columns) under `/data/live_hl_v2`.

Spreads / matrix are post-processed offline.

## Defaults

| Knob | Default |
|------|---------|
| `HL_SOCKET_COUNT` | `1` |
| `HL_CHANNELS` | `l2Book` |
| `HL_V2_PARQUET_ROOT` / `SPREAD_PARQUET_ROOT` | `/data/live_hl_v2` |
| `HL_V2_SPOOL_ROOT` | `/data/spool_hl_v2` |
| `HL_V2_GAPS_ROOT` | `/data/gaps_hl_v2` |
| Universe | `HL_V2_UNIVERSE` or `SPREAD_UNIVERSE` or repo `bybit_okx_universe.csv` |

Path helpers **refuse** `/data/live` (and other D trees). The systemd unit also sets `InaccessiblePaths=/data/live ...`.

## Schema (`hl_v2`)

Lean Bybit/OKX staff columns plus HL L1:

`event_local_ts_ms, base_coin, trigger, calc_local_ts_ms, okx_*, bybit_*, hl_*`

No `spread_*`. A row is emitted only when all three venues have a complete L1.

## How to run the experiment (code-only / later deploy)

```bash
# Local / staging tree — never against /data/live
export HL_V2_PARQUET_ROOT=/tmp/live_hl_v2
export HL_V2_SPOOL_ROOT=/tmp/spool_hl_v2
export HL_SOCKET_COUNT=1
export HL_CHANNELS=l2Book
export SPREAD_UNIVERSE=/path/to/bybit_okx_universe.csv
python -m app.hl_v2
```

Unit template (disabled, no `[Install]`):

`deploy/systemd/spread-collector-hl-v2.service`

## What NOT to do

- Do **not** set parquet root to `/data/live`.
- Do **not** `systemctl enable` / start this unit without a separate explicit deploy step.
- Do **not** merge this entrypoint into `spread-collector-next` / `app/screaner_b_o.py`.
- Do **not** delete `/data/live_hl` (prior canary data) as part of this work.
- Do **not** touch Contour B / gear2 / theta.

## Reconnect

Uses `app/utils/ws_reconnect.py` (exponential backoff + budget). On reconnect the old socket is closed before opening a new one (HL IP connection caps). Shard heartbeat logs: `ws_subscribe_ok`, `active_subs`, `last_msg_age_ms`, `reconnect_count`. Alert log: `reconnect_budget_exceeded`.

## Relation to other contours

| Contour | Entry | Root |
|---------|-------|------|
| Prod next | `app/screaner_b_o.py` | `/data/live` |
| Prior HL L1 draft (PR) | `python -m app.hl` | `/data/live-hl` |
| **This canary** | `python -m app.hl_v2` | `/data/live_hl_v2` |
## Stitch note (2026-10-01)

HL v2 contour (`app.hl_v2`) was previously only on `main_hl` @ `43cc9b3`.
This change ports the package, `hl_v2` writer schema mode, unit template, canary doc, and tests onto current `main` so the 3ex contour matches prod collector lineage without merging into `screaner_b_o.py` / `spread-collector-next`.

Isolation unchanged: roots `/data/live_hl_v2` (+ spool/gaps), `InaccessiblePaths` block prod `/data/live`.

