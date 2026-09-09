# Gear 2.2 live TW p50 watcher (BotRuntime observer)

**Track:** 3 Glue / B-bot. **Gear:** 2.2 observation on the live public market watcher.  
**Status:** async observer on `BotRuntime` — alongside the floor watcher, not instead of it. Not a trading threshold, not private WS, not tick WAL.

Code: [`app/bot/tw_p50_watcher.py`](../app/bot/tw_p50_watcher.py), wired from [`app/bot/runtime.py`](../app/bot/runtime.py) next to [`app/bot/floor_watcher.py`](../app/bot/floor_watcher.py).

## Process (words)

1. Public OKX/Bybit books update → existing coalesce → `_handle_book_sync`.
2. After tick validity and `compute_spreads`, the TW p50 observer appends `(ts_ms, spread)` per `(base_coin, side)` into an in-memory ring (same hook path as floor `note_spreads`).
3. Decide / stub place continue on the hot path unchanged — append is cheap; no p50 math and no I/O under the book lock.
4. An asyncio task (~1 Hz) copies the rings, age-prunes samples older than 5m (keeps one carry-in), and for windows **W = 60_000 ms** and **300_000 ms**:
   - builds hold segments (value held until next tick; last → window right edge);
   - **carry-in**: last sample at/before the left edge covers `[left, first_tick)` when present;
   - computes **TW p50** = duration-weighted median (same weight-CDF rule as floor / gear22 quantiles);
   - updates the RAM snapshot: `p50_1m`, `p50_5m`, `n_1m`, `n_5m`, `coverage_1m`, `coverage_5m`.
5. Journal at most 1 Hz via `asyncio.to_thread` to `{BBOT_DATA_ROOT}/tw_p50/…`. No per-tick files.

## Algorithm

Piecewise-constant L1 series. For window `[now − W, now]`:

| Piece | Mass |
|-------|------|
| Carry sample `ts ≤ left` | holds from `left` to first in-window tick (or `now`) |
| Each in-window tick | holds until next tick |
| Last segment | holds to `now` |

Leading gap without carry is **unobserved** (`coverage < 1`).  
`TW p50` = Hyndman–Fan type-7 analogue on the weight CDF at level 0.5 (shared helper with floor corridor).

## Memory bounds

| Buffer | Cap | Lifetime |
|--------|-----|----------|
| `(ts, spread)` ring per coin/side | **4096** default (`BBOT_TW_P50_SAMPLE_CAP`, else `BBOT_FLOOR_BAR_SAMPLE_CAP`) | last **5 minutes** + one carry-in |
| RAM snapshot | 1 row per coin/side | overwritten every ~1 s |

Over cap: reservoir sampling (floor-style Algorithm R); TW then approximate. Under cap + full coverage: exact.

Rough RAM: `N_coins × 2 × (cap+1) × ~16 B` → ~0.5 MB for 4 coins at default cap.

## Schema

Append-only `{BBOT_DATA_ROOT}/tw_p50/event_date=YYYY-MM-DD/metrics.jsonl`.

| Field | Notes |
|-------|--------|
| `schema_version` | `bbot.tw_p50.v1` |
| `base_coin` / `side` | Uppercase coin; `long` \| `short` |
| `ts_ms` | Window right edge (compute clock) |
| `p50_1m` / `p50_5m` | TW medians (null if no mass) |
| `n_1m` / `n_5m` | In-window sample counts (`left < ts ≤ now`); carry-in is not counted here |
| `coverage_1m` / `coverage_5m` | Observed mass / W ∈ `[0, 1]` (includes carry-in mass) |
| `computed_at_ms` | Wall clock at emit |

## Env flag

| Variable | Meaning |
|----------|---------|
| `BBOT_TW_P50_WATCH` | `1` / `0` force on/off. **Default on** for `gear2_would_send` and `canary_wal_eden` (same pattern as `BBOT_FLOOR_WATCH`). |
| `BBOT_TW_P50_SAMPLE_CAP` | Max ring samples per coin/side (default **4096**). |
| `BBOT_FLOOR_WATCH` | Independent; leave on to run **both** observers. |
| `BBOT_DATA_ROOT` | Journal root; TW metrics under `tw_p50/`, floor under `floor/`. |
| `BBOT_PROFILE` | `gear2_would_send` for the gear-2 stub contour. |
| `BBOT_BROKER` | Keep `stub` for would_send canary — no private send. |

## Isolation

- Own tree under BBOT data root only (`tw_p50/`, alongside `floor/` / `journal/` / `state/`).
- Never `/data/live`, `/data/bars`, `/data/compacted`, D backup prefixes.
- No tick files, no book dumps, no parquet WAL from this observer.
- No private broker imports.

## Canary (both floor + tw_p50, stub broker)

```bash
export BBOT_MODE=policy
export BBOT_PROFILE=gear2_would_send
export BBOT_BROKER=stub
export BBOT_COINS=BTC,ETH,SOL,XRP
export BBOT_DATA_ROOT=/data/bbot-gear2   # or ./output/bbot-tw-p50-canary
export BBOT_LOG_PATH=/var/log/spread/bbot.log   # or $BBOT_DATA_ROOT/bbot.log
# Both default on for gear2_would_send; set explicitly if desired:
export BBOT_FLOOR_WATCH=1
export BBOT_TW_P50_WATCH=1

PYTHONPATH=. python -m app.bot
# After ~1–5 minutes:
#   $BBOT_DATA_ROOT/floor/event_date=…/metrics.jsonl     (5m bar closes)
#   $BBOT_DATA_ROOT/tw_p50/event_date=…/metrics.jsonl    (~1 Hz rows)
```

Unit tests: `python -m unittest tests.test_bbot_tw_p50_watcher -v`

## Out of scope

- Private WS / live send / entry gates using p50 as threshold.
- Replacing the floor watcher or its in-bar `tw_p05`/`tw_p95` corridor.
- Collector / D storage changes.
- Starting VPS processes from this agent.
