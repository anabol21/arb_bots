# Gear 2.2 live theta screener (p50 − floor)

**Track:** 3 Glue / B-bot. **Gear:** 2.2 observation on the live public market watcher.  
**Status:** async follow-on to floor + TW-p50 observers. Not a trading threshold, not private WS, not tick WAL.

Code: [`app/bot/theta_screener.py`](../app/bot/theta_screener.py), wired from [`app/bot/runtime.py`](../app/bot/runtime.py).  
Depends on: [`docs/gear22-live-floor-watcher.md`](gear22-live-floor-watcher.md), [`docs/gear22-tw-p50-watcher.md`](gear22-tw-p50-watcher.md).

## Formula (locked)

For each `(base_coin, side)` long/short:

| Metric | Definition |
|--------|------------|
| `theta_1m` | `p50_1m − floor_tf_select_a25` |
| `theta_5m` | `p50_5m − floor` |

- `p50_*` = latest RAM snapshot from the live TW-p50 watcher (same coin/side).
- `floor` = latest **finite** gear-2.2 `floor_tf_select_a25` from the live floor observer (same coin/side).
- If floor or the corresponding p50 is non-finite → that theta field is **null** (JSON `null`).

## Process (words)

1. Floor observer closes 5m bars → stores last finite floor via read-only `last_floor(coin, side)`.
2. TW-p50 emit loop (~1 Hz) computes `p50_1m` / `p50_5m` and journals `tw_p50/`.
3. Immediately after, theta screener reads last floor + those p50 snapshots, computes theta, journals `theta/`.
4. No tick files. No private APIs. Hot path unchanged (decide/send not blocked).

## Schema

Append-only `{BBOT_DATA_ROOT}/theta/event_date=YYYY-MM-DD/metrics.jsonl`.

| Field | Notes |
|-------|--------|
| `schema_version` | `bbot.theta.v1` |
| `base_coin` / `side` | Uppercase coin; `long` \| `short` |
| `ts_ms` | Aligns with TW p50 window right edge |
| `p50_1m` / `p50_5m` | Copied from TW snapshot (null if missing) |
| `floor_tf_select_a25` | Latest finite floor (null until warm / non-finite) |
| `theta_1m` / `theta_5m` | `p50 − floor` when both finite; else null |
| `computed_at_ms` | Wall clock at emit |

## Env flag

| Variable | Meaning |
|----------|---------|
| `BBOT_THETA_WATCH` | `1` / `0` force on/off. **Default on** for `gear2_would_send` and `canary_wal_eden` (same pattern as floor / tw_p50). |
| `BBOT_FLOOR_WATCH` | Keep on so floor updates (else theta floors stay null). |
| `BBOT_TW_P50_WATCH` | Keep on so p50 updates (theta follows the 1 Hz TW emit). |
| `BBOT_DATA_ROOT` | Journal root; theta under `theta/`, beside `floor/` and `tw_p50/`. |
| `BBOT_PROFILE` | `gear2_would_send` for the gear-2 stub contour. |
| `BBOT_BROKER` | Keep `stub` for would_send canary — no private send. |

## Isolation

- Own tree under BBOT data root only (`theta/`, alongside `floor/` / `tw_p50/` / `journal/` / `state/`).
- Never `/data/live`, `/data/bars`, `/data/compacted`, D backup prefixes.
- No tick files, no book dumps, no parquet WAL from this screener.
- No private broker imports.
- RAM: only last floor + last p50 per side for compute; last theta snapshot optional.

## Canary (stub: floor + tw_p50 + theta)

```bash
export BBOT_MODE=policy
export BBOT_PROFILE=gear2_would_send
export BBOT_BROKER=stub
export BBOT_COINS=BTC,ETH,SOL,XRP
export BBOT_DATA_ROOT=/data/bbot-gear2   # or ./output/bbot-theta-canary
export BBOT_LOG_PATH=/var/log/spread/bbot.log   # or $BBOT_DATA_ROOT/bbot.log
# All three default on for gear2_would_send; set explicitly if desired:
export BBOT_FLOOR_WATCH=1
export BBOT_TW_P50_WATCH=1
export BBOT_THETA_WATCH=1

PYTHONPATH=. python -m app.bot
# After floor warm-up (≥12h SMA-12 history for finite floor) and ~1–5 minutes of books:
#   $BBOT_DATA_ROOT/floor/event_date=…/metrics.jsonl     (5m bar closes)
#   $BBOT_DATA_ROOT/tw_p50/event_date=…/metrics.jsonl    (~1 Hz)
#   $BBOT_DATA_ROOT/theta/event_date=…/metrics.jsonl     (~1 Hz; theta null until floor finite)
```

Disable: `BBOT_THETA_WATCH=0`.

Unit tests: `python -m unittest tests.test_bbot_theta_screener -v`

## Out of scope

- Private WS / live send / entry gates using theta as threshold.
- Changing floor or TW-p50 formulas.
- Collector / D storage changes.
- Starting VPS processes from this agent.
