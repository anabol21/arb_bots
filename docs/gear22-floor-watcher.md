# Gear 2.2 floor watcher (market observation journal)

**Трек:** 2 Historical model. **Гир:** 2.2 (первая полоска market trading contour).  
**Статус:** observation / would_send-style feature journal. **Не** ордера, **не** private broker, **не** live threshold, **не** классификатор режима.

Формула флора зафиксирована отдельно: [`docs/gear22-floor-metric.md`](gear22-floor-metric.md) → [`floors.compute_chosen_floor`](../research/gear22_quiet_regime_viz/floors.py). Этот модуль **не** переопределяет формулу.

Код: [`research/gear22_floor_watcher/`](../research/gear22_floor_watcher/).

## Зачем

Посчитать locked observation floor для **всего** `take=yes` рынка (~189 монет), long и short независимо, и журналировать `close`, `sma12`, `floor`, `edge = close − floor` для дальнейшей динамики / profit functional. Без Trade_Lat, без entry gates, без send.

## Формула (pointer only)

```
s_t     = SMA_12(close)_t          # 5m classic spread close
floor_t = min(trim_3h_α25, trim_12h_α25) of s   # tf-select α25
```

См. канон в `floors.py`. Warm-up / дыры — как там.

## CLI

```bash
# One-shot backfill over a window (fixture / offline)
PYTHONPATH=. python -m research.gear22_floor_watcher \
  --data-root research/fixtures/gear22_quiet_regime_viz/ticks \
  --out /tmp/floor_journal \
  --coins SOL,XRP \
  --since 2026-09-03T08:21:00Z \
  --until 2026-09-03T08:40:00Z \
  --formats parquet,jsonl

# Full take=yes universe against a local compacted dump
PYTHONPATH=. python -m research.gear22_floor_watcher \
  --data-root /path/to/compacted \
  --out /path/to/floor_market_journal \
  --universe bybit_okx_universe.csv \
  --since 2026-09-03T08:21:00Z \
  --lookback-hours 13

# Incremental / watch: re-run periodically; merge by key
PYTHONPATH=. python -m research.gear22_floor_watcher \
  --data-root /path/to/compacted \
  --out /path/to/floor_market_journal \
  --watch \
  --formats parquet
```

| Flag | Meaning |
|------|---------|
| `--data-root` | Same lean parquet layout as gear22 viz `load.py` |
| `--out` | Journal stem (writes `.parquet` / `.jsonl`) |
| `--universe` | CSV with `take` column; default repo `bybit_okx_universe.csv` |
| `--coins` | Optional override; default = all `take=yes` |
| `--since` / `--until` | Emit window (UTC). Default since = gear2 restart; until = now |
| `--lookback-hours` | Extra history before since for SMA-12 / 12h trim warm-up (default 13) |
| `--watch` | Resume from last journal `bar_end_ms` when `--since` omitted; always merge by key |
| `--formats` | `parquet`, `jsonl`, or both |

VPS systemd deploy **out of scope** for this PR. Later: point `--data-root` at a copy of compacted ticks (or a mount-visible path) and run oneshot / cron `--watch` offline — no WS ingest in v1.

## Output schema

One row per `(bar_end_ms, base_coin, side)`:

| Column | Type | Notes |
|--------|------|-------|
| `bar_end_ms` | int64 | End of 5m bar (UTC ms) |
| `bar_start_ms` | int64 | Start of 5m bar |
| `base_coin` | str | Uppercase |
| `side` | str | `long` \| `short` |
| `close` | float | Classic spread close (`spread_long` / `spread_short`) |
| `sma12` | float | Causal SMA-12 of close |
| `floor_tf_select_a25` | float | Chosen floor (`tf-select α25`) |
| `edge` | float | `close - floor` when both finite, else NaN |
| `tick_count` | int | Ticks in bar (0 = empty bucket) |
| `formula_id` | str | `tf-select-a25-of-sma12` |
| `computed_at_ms` | int64 | Wall clock of this run |
| `data_root` | str | Input root used |
| `since_ms` / `until_ms` | int64 | Emit window |
| `lookback_ms` | int64 | Loaded history before since |
| `run_mode` | str | `oneshot` \| `watch` |

Idempotency: re-runs merge with last-wins on `(bar_end_ms, base_coin, side)`.

## Что это не закрывает

- Ордера / private broker / live send.
- Trade_Lat, profit functional, entry gates.
- Live WS ingest (ticks-from-parquet достаточно для v1).
- VPS systemd unit / deploy.
- Утверждение, что floor = тихий уровень рынка.
