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
| `--out-dir` | Output directory for per-coin HTML |
| `--gap-threshold-ms` | Inter-tick gap mark threshold (default `30000`) |
| `--ma-bars` | Causal SMA windows in **5m bars** (default `3,12` → 15m / 60m) |
| `--max-tick-points` | Even downsample cap for sparse tick overlay (default `4000`) |

### `--since` = last restart

Default `--since` is the documented live gear2 restart:

- `2026-09-03T08:21:00Z` (UTC)
- `2026-09-03 11:21` MSK

Override when the process restarts again. The visualizer does not read VPS clocks.

Typical VPS inputs (copy off-box first):

- compacted L1: `/data/compacted/spread_*.parquet`
- (journals under `/data/bbot-gear2/…` are **not** required for this observer)

## What each page shows

1. **Primary — OKX−Bybit mid edge (%)** as UTC-aligned **5m OHLC candles**:
   `edge_pct = (okx_mid − bybit_mid) / bybit_mid × 100`
2. **Causal SMAs** on candle **closes** (default SMA-3 and SMA-12 of 5m bars).
3. **Sparse ticks** overlay (even downsample) so microstructure / holes stay visible.
4. **Red vrects** where consecutive ticks are farther apart than `--gap-threshold-ms`.
5. **Mid context** panel: OKX mid and Bybit mid.
6. **Intra-candle (same 5m bucket) stats**:
   - tick count (hover includes `update_rate_hz`)
   - gap fraction = (time before first tick + time after last tick) / 300s
   - mean ± std of edge
   - min / max / IQR (q25–q75)

Layout choice: edge as the research primary (quiet thresholds will apply to edge /
spread-like series), mids as price context — not a trading-terminal clone.

## Gap definition

A **red gap region** is drawn for every pair of consecutive ticks `(t_i, t_{i+1})`
with `t_{i+1} − t_i > gap_threshold_ms` (default 30s). The band spans
`[t_i, t_{i+1})`.

Intra-bucket **gap_fraction** is separate: for each 5m bucket it measures how much
of the bucket lies outside the first→last tick extent (empty bucket → `1.0`).

## Moving averages

Defaults are **causal** (right-aligned) SMAs of 5m **closes**:

| Name | Window |
|------|--------|
| SMA-3×5m | last 3 completed finite closes (~15m) |
| SMA-12×5m | last 12 completed finite closes (~60m) |

Empty buckets (NaN close) break the MA until a full finite run rebuilds. Override
with `--ma-bars 6,24` etc.

## Adding a future integral metric

Do **not** invent placeholder series. Implement real candidates in:

`research/gear22_quiet_regime_viz/metrics_ext.py` → `collect_extension_traces`

Return `MetricTrace` objects with `panel` in
`{edge, mid, tick_count, gap_fraction, mean_std, range_iqr}`. Each HTML page
embeds a short extension-help section describing this hook.

## Dependencies

- `pandas`, `numpy`, `pyarrow`, `plotly`

(Already used elsewhere in `research/`.)

## Module map

| Module | Role |
|--------|------|
| `load.py` | Discover / read parquet\|CSV, derive mid + edge + spreads |
| `candles.py` | 5m OHLC + intra-stats + causal SMA |
| `gaps.py` | Inter-tick gap intervals |
| `plot.py` | Plotly multi-subplot HTML writer |
| `metrics_ext.py` | Empty extension hook |
| `cli.py` / `__main__.py` | CLI entry |

## Tests

```bash
PYTHONPATH=. python -m unittest tests.test_gear22_quiet_regime_viz -v
```
