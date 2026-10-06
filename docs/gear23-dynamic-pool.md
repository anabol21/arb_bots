# Gear 2.3 dynamic pool — Patch A

Gear 2.3 reuses would-send's cumulative hot-add CSV contract for the Gear 2.2 public market-data contour. The fixed configured pool remains the base. A delta snapshot can append candidates, starting one OKX books5 and one Bybit orderbook.1 public stream per accepted coin. Metadata is resolved through the same fail-closed lot/tick gate as would-send.

Patch A is public-only. It leaves every added coin out of the trade manager's coin order; a candidate can be observed and warmed but cannot be opened. It does not add private subscriptions, establish leverage, verify account settings, or enable order sending. A later patch must explicitly add readiness and eligibility gates before any candidate can reach the manager.

## Opt-in configuration

`BBOT_HOT_ADD=1` is off by default. Patch A accepts it only for a Gear 2.2 profile with `BBOT_BROKER=stub`, `LIVE_ORDERS` false, `BBOT_THETA_LIVE_SEND` false, and non-terminal-private execution. For a manual experiment use an isolated `BBOT_DATA_ROOT`, a separate `BBOT_HOT_ADD_DELTA` snapshot, and an environment without exchange credentials. Do not point it at the production would-send delta or share a live canary run root.

| Variable | Default | Meaning |
|---|---:|---|
| `BBOT_HOT_ADD` | off | Enable the public-only poller. |
| `BBOT_HOT_ADD_DELTA` | `hot_add_delta.csv` | Snapshot path; relative paths resolve under `BBOT_DATA_ROOT`. |
| `BBOT_HOT_ADD_MAX_EXTRA` | 8 | Maximum added coins. Set to the would-send configured cap for comparisons. |
| `BBOT_HOT_ADD_POLL_SEC` | 30 | Snapshot poll interval. |
| `BBOT_HOT_ADD_WARM` | on | Attempt read-only observer warm from available history. |
| `BBOT_HOT_ADD_HISTORY_ROOT` | data root | Optional alternate read-only warm-history root. |

The snapshot uses the existing `app/utils/universe_delta.py` columns. A candidate absent from the loaded universe must carry positive lot/tick/minimum metadata for both venues. Invalid metadata is logged and skipped. Missing snapshots do not remove the base pool. The production parser is cumulative and idempotent; Patch A is append-only and deliberately has no drop file behavior.

## Patch A checks

Offline checks are `python3 -m unittest tests.test_gear23_hot_add` and `python3 -m py_compile app/bot/runtime.py app/bot/hot_add.py app/bot/hot_add_warm.py`. The bounded runtime experiment, after review, should use a dedicated run directory and a manually written CSV with one new candidate, then a second candidate. Confirm two public subscriptions per accepted candidate, duplicate snapshot no-ops, invalid metadata skip, explicit observer warm result, and unchanged trade-eligible base pool. Keep `LIVE_ORDERS=0`; no private session or exchange order is part of A.

Patch B must separately validate private subscription readiness, candidate metadata/1x preparation, fresh public generations, observer warm state, held/pending coin retention, and trade eligibility. It must reuse the active would-send selector's existing cumulative snapshot rather than run a second selector.
