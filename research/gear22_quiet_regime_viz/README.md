# Gear 2.2 quiet-regime research visualizer

Offline Plotly HTML observer for **gear 2.2** quiet-regime / noisy-gappy L1 work.

One HTML page per coin. No VPS required — point `--data-root` at a local
compacted tick dump (or the tiny fixture under `research/fixtures/…`).

This tool **does not** set live bot thresholds and **does not** invent quiet-regime
floor metrics. Extension hooks are empty stubs for later integral candidates.

## Quick start (fixture / CI smoke)

```bash
cd /path/to/arb_bots
PYTHONPATH=. python -m research.gear22_quiet_regime_viz \
  --data-root research/fixtures/gear22_quiet_regime_viz/ticks \
  --coins SOL,XRP \
  --since 2026-09-03T08:21:00Z \
  --until 2026-09-03T08:40:00Z \
  --out-dir /tmp/gear22_viz
```

Open `/tmp/gear22_viz/gear22_quiet_regime_SOL.html` (and `_XRP.html`).
Keep the sibling `plotly.min.js` next to the HTML (copied automatically).
Also written: `coins.json` (stable nav list).

Regenerate the fixture:

```bash
PYTHONPATH=. python -m research.gear22_quiet_regime_viz.build_fixture
```

## CLI

| Flag | Meaning |
|------|---------|
| `--data-root` | Local root with `spread_*.parquet`, hive `base_coin=*/event_date=*/`, or CSV |
| `--coins` | Comma list (default `SOL,XRP`; add `BTC,ETH` as needed) |
| `--since` | Window start — **last restart** timestamp (see below) |
| `--until` | Optional end (default: now UTC) |
| `--out-dir` | Output directory for per-coin HTML + `plotly.min.js` + `coins.json` |
| `--gap-threshold-ms` | Inter-tick gap mark threshold (default `30000`) |
| `--ma-bars` | Causal SMA windows in **5m bars** (default `3,12` → 15m / 60m) |
| `--max-tick-points` | Even downsample cap for sparse tick overlay (default `4000`) |
| `--candle-bins` | In-bar **TW-mass** hist bins for click-to-inspect (default `32`; `0` disables) |
| `--candle-temporal-bins` | Equal-time mean slots per 5m bar (default `16`) |
| `--latency-bins` | In-bar **trigger-venue** latency hist bins, **equal-weight tick counts** (default `24`; `0` disables) |
| `--latency-temporal-bins` | Equal-time mean slots for latency temporal view (default `12`) |
| `--inline-plotly` | Embed plotly.js inside each HTML (large single-file). Default = sibling `plotly.min.js` |

### `--since` = last restart

Default `--since` is the documented live gear2 restart:

- `2026-09-03T08:21:00Z` (UTC)
- `2026-09-03 11:21` MSK

Override when the process restarts again. The visualizer does not read VPS clocks.

Typical VPS inputs (copy off-box first):

- compacted L1: `/data/compacted/spread_*.parquet`

### `plotly.min.js`

By default each page loads `<script src="plotly.min.js">`. The writer **always
copies** `plotly.min.js` into `--out-dir` beside the HTML. Open pages via
`file://` only with that sibling present (or pass `--inline-plotly` for a
self-contained but much larger HTML).

## Long vs short (policy-aligned)

Primaries match `app.policy.features` / gear2 `open_long` vs `open_short`
(not a signed mid-edge):

| Side | Formula | Policy action |
|------|---------|---------------|
| **long** `spread_long` | `(bybit_bid − okx_ask) / bybit_bid × 100` | `open_long` |
| **short** `spread_short` | `(okx_bid − bybit_ask) / okx_bid × 100` | `open_short` |

Each coin page stacks: shared mid context → **LONG** block (candles, MAs, sparse
ticks, red gaps, intra-bucket stats, TW quantiles, window histogram) → **SHORT**
block (same stack). Mid-edge `edge_pct` is still derived in the loader for
context but is not the dual-stack primary.

## What each page shows

1. **Coin nav** — ←/→ links, keyboard arrows, swipe; wraps at ends; `file://`-safe
   relative hrefs. Stable list embedded in the page + sibling `coins.json`.
2. **Mid context** (OKX mid + Bybit mid) once at the top.
3. Per side (**long**, then **short**):
   - UTC-aligned **5m OHLC candles** of that spread
   - **Causal SMAs** on candle closes (default SMA-3 / SMA-12)
   - **Sparse ticks** overlay
   - **Red vrects** for inter-tick gaps `> --gap-threshold-ms`
   - Intra-bucket **tick count** (hover: `update_rate_hz`)
   - **gap_fraction** = extent uncovered to bucket edges / 300s (not inter-tick holes)
   - mean ± std; min / max / IQR (equal-weight ticks)
   - **Time-weighted p25 / p50 / p95 / p99** as chart series
   - **Window histogram** of all ticks in `--since`/`--until` (equal weight)
4. **Click-to-inspect (in-bar)** — click a 5m candlestick (or its MA / sparse-tick
   overlay in the candle row) → fixed bottom panel with
   (a) **time-weighted mass** histogram of that bar’s spread (robust x-range ≈
   TW p01–p99; rare spikes no longer stretch the axis to empty space),
   (b) **TW p50 / p95 / p99** (and mean) in the subtitle + vlines on the hist,
   (c) equal-time bin means (temporal; not TW — see below), and
   (d) **venue latency of the triggering venue only** — equal-weight tick-count
   hist + equal-time means. A tick with `trigger=="okx"` contributes only
   `okx_latency_ms`; `trigger=="bybit"` contributes only `bybit_latency_ms`.
   The other book’s latency is never put into that venue’s inspect hist.
   Compact payloads are computed at **build time** from full ticks and attached as
   candlestick `customdata` (tens of bins, not raw ticks). Clicks on overlays resolve
   the bar by time against that `customdata` (overlays sit above the candle trace).
   Prefer click over hover. Disable spread inspect with `--candle-bins 0`; latency
   with `--latency-bins 0`.

### Inspect payload weighting

| Field | Weighting | Notes |
|-------|-----------|-------|
| spread `c` (hist bins) | **TW mass (ms)** | Hold-until-next-tick; last → bar end. Key `c_w=tw_ms`. |
| spread `tw.mean` / `tw.p50` / `tw.p95` / `tw.p99` | **TW** | Same hold rule as `quantiles.py`. |
| spread `lo` / `hi` | TW p01–p99 (+ pad) | Robust axis; outliers clipped into edge bins. |
| `tv` (temporal) | Equal-time slot means | Documented `tv_w=equal_time`; not TW. |
| latency `lat.okx` / `lat.bybit` `c` | **Equal-weight tick counts** | Key `c_w=count`. `sum(c) == n` (finite trigger-scoped ticks), not ms. |
| latency `tw.*` (same compact keys) | **Equal-weight** | Mean / p01 / p50 / p95 / p99 over trigger-scoped ticks only. |
| latency `lo` / `hi` | Equal-weight p01–p99 (+ pad) | Trigger-scoped samples only. |

Spread: equal-weight tick counts + `hi = max` (rare spike) caused crushed-left /
empty-right hists; TW mass + robust range is the fix. Latency is **not**
time-weighted: a long-held stale other-book latency must not dominate.

### Latency trigger scope

`n_okx` and `n_bybit` in a bar are **trigger counts** (finite samples), not the
spread tick count. They must be able to differ when one venue fires more often.

| Tick `trigger` | Used in inspect | Ignored for that tick |
|----------------|-----------------|------------------------|
| `okx` | `okx_latency_ms` only | `bybit_latency_ms` |
| `bybit` | `bybit_latency_ms` only | `okx_latency_ms` |

`trigger` is loaded via `load.py` (`_READ_COLS`) and kept on the working frame.
If `trigger` is missing (legacy dump), latency payloads are **omitted** (`lat`
absent) — unscoped every-tick latency would mix “freshness of the other book”
with the venue that fired the row. Non-finite values are still skipped (`n`
counts only finite trigger-scoped samples).

### Latency columns

Derived in `load.derive_research_series` (same convention as `research/lean_ticks_io`):

| Column | Definition |
|--------|------------|
| `okx_latency_ms` | `okx_local_recv_ts_ms − okx_ts_ms` |
| `bybit_latency_ms` | `bybit_local_recv_ts_ms − bybit_ts_ms` |

If precomputed latency columns already exist in the dump, they are kept (numeric
coerce). If source timestamps are missing for a venue, that venue’s latency key is
omitted from the inspect payload. Non-finite latency values are **skipped**.
Inspect uses a venue’s latency **only on ticks that venue triggered**.

## Time-weighted quantile convention

Inside each UTC 5m bucket `[bar_start, bar_end)`:

1. Sort ticks by `event_local_ts_ms`.
2. Weight of tick `i` = time until the next tick (`t_{i+1} − t_i`).
3. Weight of the **last** tick = `bar_end − t_last` (clamped ≥ 0).
4. Leading gap before the first tick is **unobserved** (no mass).
5. Quantiles are read from the cumulative weight CDF of the held values
   (piecewise-constant step function).

Same hold rule can be applied to a whole window with `last → until`.

## Gap definition

A **red gap region** is drawn for every pair of consecutive ticks `(t_i, t_{i+1})`
with `t_{i+1} − t_i > gap_threshold_ms` (default 30s). The band spans
`[t_i, t_{i+1})`. These are the real holes.

Intra-bucket **gap_fraction** is separate: fraction of the 5m bucket outside the
first→last tick extent (empty → `1.0`). It is **not** the inter-tick hole score.

## Coin navigation

- Pages embed `CFG = {coin, coins}` and sibling `coins.json`.
- ← / → (and swipe left/right) go to previous/next coin via relative
  `gear22_quiet_regime_<COIN>.html` links.
- **Wraps** at the ends (last → first).
- Works under `file://` (no web server). Nav list = coins that actually got a page,
  in `--coins` order.

## Moving averages

Defaults are **causal** (right-aligned) SMAs of 5m **closes**:

| Name | Window |
|------|--------|
| SMA-3×5m | last 3 completed finite closes (~15m) |
| SMA-12×5m | last 12 completed finite closes (~60m) |

Empty buckets (NaN close) break the MA until a full finite run rebuilds.

## Adding a future integral metric

Do **not** invent placeholder series. Implement real candidates in:

`research/gear22_quiet_regime_viz/metrics_ext.py` → `collect_extension_traces`

Return `MetricTrace` with `panel` in
`{candles, tick_count, gap_fraction, mean_std, range_iqr, tw_quantiles}`
(optional `long_` / `short_` prefix).

## Dependencies

- `pandas`, `numpy`, `pyarrow`, `plotly`

## Module map

| Module | Role |
|--------|------|
| `load.py` | Discover / read parquet\|CSV, derive mid + spreads, keep `trigger` |
| `candles.py` | 5m OHLC + intra-stats + causal SMA + TW quantile columns + click-inspect payloads |
| `quantiles.py` | Hold weights + TW quantile / mean / hist helpers |
| `gaps.py` | Inter-tick gap intervals |
| `plot.py` | Plotly multi-block HTML writer + coin nav + candle click inspect |
| `metrics_ext.py` | Empty extension hook |
| `cli.py` / `__main__.py` | CLI entry |

## Tests

```bash
PYTHONPATH=. python -m unittest tests.test_gear22_quiet_regime_viz -v
```
