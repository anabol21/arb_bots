# Feature hive for gear 2.2 plots (`by_date`)

**Track:** 2 Historical model. **Gear:** 2.2 observation.  
Not a simulator gate, not live, not alpha.

`index.html` does **not** read parquet. It loads pre-rendered PNGs. The notebook cell that builds those PNGs reads the **date-first** hive below.

## What the gallery loads

| Path | Role |
|------|------|
| `output/gear22_feature_plots/index.html` | Flip-through UI; embedded `PAGES` list |
| `output/gear22_feature_plots/img/<YYYY-MM-DD>/<COIN>.png` | One page per `(UTC day, coin)` |
| Builder | `research/gear22_feature_plot_gallery.py` + cell in `model_gear2.2.ipynb` |

Builder reads:

```text
output/gear22_backtest_features_by_date/event_date=<YYYY-MM-DD>/part-000.parquet
```

then filters `usable_long & usable_short`, derives `event_date` from `ts_s`, and plots `PLOT_COLS`:

`event_date`, `theta_1m_long/short`, `p50_1m_long/short`, `floor_long/short`.

5m candles come from a different hive (`output/{bybit,okx}_bar5m_hist_regime/base_coin=<COIN>/event_date=<D>/part.parquet`), not from the feature table.

## Producer vs consumer hive

Same schema `gear22_bt_features_v1`, same wide `(coin, ts_s)` rows. Only partition and row order change.

| | Producer (VPS + Mac writer) | Consumer (`by_date`) |
|--|--|--|
| Root | `output/gear22_backtest_features/` or `/data/experiments/gear22_bt_features/` | `output/gear22_backtest_features_by_date/` |
| Layout | `coin=<SYM>/event_date=<D>/part-000.parquet` | `event_date=<D>/part-000.parquet` |
| One file | one coin, one UTC day (or a named shard) | all 30 canary coins, time-major `(ts_s, coin)` |
| Extra shards | `part-001.parquet` when a day is split (Mac `08-27` 00:00–12:00 = `part-000`; VPS afternoon = `part-001`) | one concat file per day |

Columns (body; `event_date` is a directory key, not a parquet column):

`ts_s`, `coin`, then per side `long`/`short`: `p50_1m`, `floor`, `theta_1m`, `gapfrac5m`, `cov_1m`, `n_ticks_1m`, `usable`, `spread_last`, `spread_min`, `spread_max`, `dp50_60`, `occ60`.

Do **not** rewrite VPS output. Keep `coin=/event_date=` there. Re-partition on Mac after rsync.

## Convert (already in repo)

```bash
cd /Users/mishatrubik/Desktop/spread
PYTHONPATH=. ./venv/bin/python research/gear22_repartition_features_by_date.py
# after rsync of VPS shards that include part-001:
PYTHONPATH=. ./venv/bin/python research/gear22_repartition_features_by_date.py \
  --all-parts --overwrite --dates 2026-08-27
```

`--all-parts` concatenates `part-*.parquet` per coin/day (lexical: `part-000` then `part-001`). Default still reads only `--part-name` (`part-000.parquet`).

Mac-local August (`2026-08-10T00:00Z` → `2026-08-27T12:00Z`) is already in `by_date` (18 days; last day 12h / 1 296 000 rows). Do not rebuild unless `--overwrite`.

## Optional VPS pull (feature parquet only; skipped here)

SSH `root@38.180.94.108` works. VPS hive is still `coin=/event_date=`, ~575 MB for `2026-08-27` (afternoon `part-001` only) through `2026-09-08`. That is not a small rsync; plots also need a notebook PNG rebuild. Documented, not run:

```bash
# do not overwrite Mac 08-10..08-27 part-000; 08-27 afternoon is VPS part-001
rsync -avz --bwlimit=2000 \
  --include='MANIFEST.json' --include='MANIFEST_BACKUP_GAPS.json' \
  --include='coin=**/' --include='coin=*/event_date=**/' \
  --include='coin=*/event_date=*/*.parquet' --exclude='*' \
  root@38.180.94.108:/data/experiments/gear22_bt_features/ \
  /Users/mishatrubik/Desktop/spread/output/gear22_backtest_features/

PYTHONPATH=. ./venv/bin/python research/gear22_repartition_features_by_date.py \
  --all-parts --overwrite \
  --dates 2026-08-27 2026-08-28 2026-08-29 2026-08-30 2026-08-31 \
          2026-09-01 2026-09-02 2026-09-03 2026-09-04 2026-09-05 \
          2026-09-06 2026-09-07 2026-09-08
```

Do not rsync ticks. Do not download `backup1tb`. Collector stays frozen.
