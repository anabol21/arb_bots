# Gear 2.2 live floor watcher (BotRuntime observer)

**Track:** 3 Glue / B-bot. **Gear:** 2.2 observation on the live public market watcher.  
**Status:** async observer on `BotRuntime` — not a trading threshold, not private WS, not tick WAL.

Canonical formula: [`docs/gear22-floor-metric.md`](gear22-floor-metric.md) → [`floors.compute_chosen_floor`](../research/gear22_quiet_regime_viz/floors.py).  
Code: [`app/bot/floor_watcher.py`](../app/bot/floor_watcher.py), wired from [`app/bot/runtime.py`](../app/bot/runtime.py).

## Process (words)

1. Public OKX/Bybit books update → existing coalesce → `_handle_book_sync`.
2. After tick validity and `compute_spreads`, the observer notes `spread_long` / `spread_short` in memory (last close + tick count only).
3. Decide / stub place continue on the hot path unchanged.
4. When the UTC 5m bucket rolls, the observer closes the prior bar: causal SMA-3, SMA-12, then `compute_chosen_floor(sma12_hist)` → `floor_tf_select_a25`.
5. Closed metric rows are queued under the book lock; JSONL append runs in `asyncio.to_thread` **after** the lock (does not block decide/send).
6. No private broker imports on this path. No per-tick journal. No writes into D trees (`/data/live`, `/data/bars`, `/data/compacted`, spool).

Memory caps: deque of 12 closes + deque of 144 SMA-12 values per coin/side.

## Schema

Append-only `{BBOT_DATA_ROOT}/floor/event_date=YYYY-MM-DD/metrics.jsonl`.

One row per `(bar_end_ms, base_coin, side)` on bar close:

| Field | Notes |
|-------|--------|
| `schema_version` | `bbot.floor.v1` |
| `formula_id` | `tf-select-a25-of-sma12` |
| `base_coin` / `side` | Uppercase coin; `long` \| `short` |
| `bar_start_ms` / `bar_end_ms` | UTC 5m bucket |
| `close` | Last valid spread in bar |
| `sma3` / `sma12` | Causal SMAs of closes (null until warm) |
| `floor_tf_select_a25` | Chosen floor (null until 12h warm-up) |
| `edge` | `close − floor` when both finite |
| `tick_count` | Valid spreads in the closed bar |
| `computed_at_ms` | Wall clock at close |
| `event_date` | UTC date of `bar_end_ms` |

## Env flag

| Variable | Meaning |
|----------|---------|
| `BBOT_FLOOR_WATCH` | `1` / `0` force on/off. **Default on** for `gear2_would_send` and `canary_wal_eden`; off for `gear1` / `signal_test`. |
| `BBOT_DATA_ROOT` | Journal root (e.g. `/data/bbot-gear2`); floor metrics under `floor/`. |
| `BBOT_PROFILE` | Use `gear2_would_send` for the gear-2 contour. |
| `BBOT_BROKER` | Keep `stub` (or omit) for would_send canary — no private send. |

## Isolation

- Own tree under BBOT data root only (`floor/`, existing `journal/` / `state/`).
- Never `/data/live`, `/data/bars`, `/data/compacted`, D backup prefixes.
- No tick files, no book dumps, no parquet WAL from this observer.
- Formula lives in `research/.../floors.py` — not forked in the bot module.

## Canary (stub broker, send=false)

Short would_send run to confirm metric rows without OOM:

```bash
# From repo root on the VPS (or local with writable BBOT_DATA_ROOT)
export BBOT_MODE=policy
export BBOT_PROFILE=gear2_would_send
export BBOT_BROKER=stub
export BBOT_COINS=BTC,ETH,SOL,XRP   # or a single coin for a tighter RSS check
export BBOT_DATA_ROOT=/data/bbot-gear2   # or ./output/bbot-floor-canary
export BBOT_LOG_PATH=/var/log/spread/bbot.log   # or $BBOT_DATA_ROOT/bbot.log
# BBOT_FLOOR_WATCH defaults on for gear2_would_send; set =0 to disable

PYTHONPATH=. python -m app.bot
# Wait >5 minutes of valid books (one bar close), then:
#   ls $BBOT_DATA_ROOT/floor/event_date=*/metrics.jsonl
#   tail -n 2 $BBOT_DATA_ROOT/floor/event_date=*/metrics.jsonl
# Expect sma3 / sma12 / floor_tf_select_a25 fields; no *tick* files under data root.
# RSS should stay flat aside from normal book state — no tick history growth.
```

Unit tests: `python -m unittest tests.test_bbot_live_floor_watcher -v`

## Out of scope

- Private WS / live send / entry gates using floor as threshold.
- Offline compacted oneshot contour (`research/gear22_floor_watcher`).
- Collector / D storage changes.
