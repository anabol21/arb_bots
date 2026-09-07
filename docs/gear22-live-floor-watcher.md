# Gear 2.2 live floor watcher (BotRuntime observer)

**Track:** 3 Glue / B-bot. **Gear:** 2.2 observation on the live public market watcher.  
**Status:** async observer on `BotRuntime` — not a trading threshold, not private WS, not tick WAL.

Canonical formula: [`docs/gear22-floor-metric.md`](gear22-floor-metric.md) → [`floors.compute_chosen_floor`](../research/gear22_quiet_regime_viz/floors.py).  
Code: [`app/bot/floor_watcher.py`](../app/bot/floor_watcher.py), wired from [`app/bot/runtime.py`](../app/bot/runtime.py).  
Canary plots: [`app/bot/floor_plot.py`](../app/bot/floor_plot.py).

## Process (words)

1. Public OKX/Bybit books update → existing coalesce → `_handle_book_sync`.
2. After tick validity and `compute_spreads`, the observer notes `spread_long` / `spread_short` in memory (last close + open-bar sample list).
3. Decide / stub place continue on the hot path unchanged.
4. When the UTC 5m bucket rolls, the observer closes the prior bar:
   - causal SMA-3, SMA-12, then `compute_chosen_floor(sma12_hist)` → `floor_tf_select_a25`
   - **TW `tw_p05` / `tw_p95`** of in-bar spreads (hold→next; last→bar end — same convention as gear22 viz)
   - discard open-bar samples (no tick WAL)
5. Closed metric rows are queued under the book lock; JSONL append runs in `asyncio.to_thread` **after** the lock (does not block decide/send).
6. No private broker imports on this path. No per-tick journal. No writes into D trees (`/data/live`, `/data/bars`, `/data/compacted`, spool).

### Memory bounds

| Buffer | Cap | Lifetime |
|--------|-----|----------|
| closes | 12 | across bars (SMA) |
| sma12_hist | 144 | across bars (12h floor) |
| open-bar `(ts, spread)` | **4096** default (`BBOT_FLOOR_BAR_SAMPLE_CAP`) | **current bar only**; cleared on close |

Under the cap, all valid spreads in the bar are kept (exact TW). Over the cap: reservoir sampling — TW becomes an approximation; `samples_seen` / `samples_kept` record that.

### Corridor method

**Time-weighted** p05 / p95 (not equal-count). Matches gear22 `tick_hold_weights_ms` + weight-CDF quantile. Field names: `tw_p05`, `tw_p95` (consistent with viz `tw_*` naming).

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
| `tw_p05` / `tw_p95` | In-bar TW corridor edges (null if no samples) |
| `edge` | `close − floor` when both finite |
| `tick_count` | Valid spreads in the closed bar |
| `samples_kept` / `samples_seen` | Kept vs seen (cap / reservoir) |
| `computed_at_ms` | Wall clock at close |
| `event_date` | UTC date of `bar_end_ms` |

## Env flag

| Variable | Meaning |
|----------|---------|
| `BBOT_FLOOR_WATCH` | `1` / `0` force on/off. **Default on** for `gear2_would_send` and `canary_wal_eden`; off for `gear1` / `signal_test`. |
| `BBOT_FLOOR_BAR_SAMPLE_CAP` | Max open-bar samples per coin/side (default **4096**). |
| `BBOT_DATA_ROOT` | Journal root (e.g. `/data/bbot-gear2`); floor metrics under `floor/`. |
| `BBOT_PROFILE` | Use `gear2_would_send` for the gear-2 contour. |
| `BBOT_BROKER` | Keep `stub` (or omit) for would_send canary — no private send. |

## Isolation

- Own tree under BBOT data root only (`floor/`, existing `journal/` / `state/`).
- Never `/data/live`, `/data/bars`, `/data/compacted`, D backup prefixes.
- No tick files, no book dumps, no parquet WAL from this observer.
- Formula lives in `research/.../floors.py` — not forked in the bot module.

## Canary (stub broker, send=false)

```bash
export BBOT_MODE=policy
export BBOT_PROFILE=gear2_would_send
export BBOT_BROKER=stub
export BBOT_COINS=BTC,ETH,SOL,XRP
export BBOT_DATA_ROOT=/data/bbot-gear2   # or ./output/bbot-floor-canary
export BBOT_LOG_PATH=/var/log/spread/bbot.log   # or $BBOT_DATA_ROOT/bbot.log

PYTHONPATH=. python -m app.bot
# After overnight / several 5m closes:
PYTHONPATH=. python -m app.bot.floor_plot \
  --data-root "$BBOT_DATA_ROOT" \
  --out /tmp/floor-canary-plots \
  --coins BTC,ETH,SOL,XRP
# Open *_floor_canary.html (Plotly CDN). PNG written only if matplotlib is installed.
```

Unit tests: `python -m unittest tests.test_bbot_live_floor_watcher -v`

## Out of scope

- Private WS / live send / entry gates using floor as threshold.
- Offline compacted oneshot contour (`research/gear22_floor_watcher`).
- Collector / D storage changes.
- Starting VPS processes from this agent.
