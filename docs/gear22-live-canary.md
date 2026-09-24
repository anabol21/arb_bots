# Gear 2.2 live canary (Contour B dual-leg)

**Track:** 3 Glue / B-private. **Gear:** 2.2 observation policy → parallel **live** canary.  
**Status:** legacy code + operator template only. **Do not deploy or enable on VPS from this change.**
This template predates the
[EV2-12→EV2-14 private-production bridge](task-packets/EV2-12-to-gear22-private-prod-bridge.md).
Its dual-ACK local slot is not a confirmed-filled or confirmed-flat proof;
the bridge's exposure/WAL/reconciliation gates supersede any live-readiness
implication here.
**Not** a profitability claim and **not** a replacement for the stub would_send unit.

Parallel live canary: **same HTML top30** and **frozen policy knobs** as VPS unit
`spread-bbot-theta-k1-canary`. Revised future-live ceiling is **$10 USDT per leg**. Send is Contour B
trivial dual-leg (default private send path). Chronometry on. Sentry project
Contour B (DSN only in the secret env file).

The existing would_send stub unit stays running **unchanged** (separate profile,
flags, and data root). Compare later via shared `trade_id`.

## Isolation

| | Live canary | would_send (do not change) | D |
|--|-------------|----------------------------|---|
| Unit (template) | `spread-bbot-gear22-live-canary.service` | `spread-bbot-theta-k1-canary` (VPS) | `spread-collector` |
| Profile | `gear22_live_canary` | `gear22_would_send` | — |
| Broker | `private_live` → Contour B | `stub` | — |
| Data | `/data/bbot-gear22-live-canary` | `/data/bbot-theta-k1-canary` | `/data/live`, `/data/bars`, … |
| Log | `/var/log/spread/bbot-gear22-live-canary.log` | stub canary log | `runtime.log` |
| Notional | **10** USDT / leg | 10 today (leave it) | — |

Never write D trees. Never start/stop collector. Do not `systemctl enable`
this unit from the repo file alone. Do not touch the would_send unit.

## Fail-closed live gate

Live send is **off** unless:

- `BBOT_PROFILE=gear22_live_canary` (alias `gear22_live`), **or**
- `BBOT_THETA_LIVE_SEND=1`

When requested, startup **raises** unless all of:

- `BBOT_BROKER=private_live`
- `VENUE=live`
- `LIVE_ORDERS=1`

`BBOT_BROKER=stub` / no `LIVE_ORDERS` → current would_send behavior (70 ms
synthetic fill, `send=false`, no `LiveBroker.place`).

W6 is **not** on the hot path. Do not set `BBOT_PRIVATE_SEND_PATH=w6`.

## Decide → send

Same `policy.decide` + feature snapshot path as would_send. On open/close that
pass size_check at **$10/leg**:

1. Place Contour B dual-leg **immediately at signal** (no 70 ms sleep; 70 ms
   remains would_send-only fill model).
2. One UUID is theta `trade_id` **and** live open `intent_id` / dual-leg
   correlation (`clOrdId` / `orderLinkId` derived from that id).
3. ACK-aware open: both trade ACKs or flatten the accepted/timed-out open leg
   (Contour B; do not leave one-leg inventory). Local slot stays flat on abort.
4. Chronometry **after** dual ACK (`BBOT_CHRONOMETRY` on for this profile).
5. OKX VIP `fills` stays unsubscribed; chronometry reads `orders`.

Theta journal still writes `would_send=true`. Successful live place sets
`send=true` and `intent_id`. Failed place journals `send=false` + `live_abort`.

## Operator env

### would_send (existing unit — do not change)

```text
BBOT_MODE=policy
BBOT_PROFILE=gear22_would_send
BBOT_BROKER=stub
BBOT_THETA_TRADE=1
# BBOT_THETA_LIVE_SEND unset or 0
BBOT_NOTIONAL_USDT=10
BBOT_DATA_ROOT=/data/bbot-theta-k1-canary
BBOT_COINS=KAITO,HOME,WAL,RVN,ONT,2Z,BICO,HMSTR,CAP,BLEND,EDEN,KMNO,GPS,ME,ZBT,MOVE,COAI,AZTEC,APR,YB,ICX,AT,H,MUBARAK,ACU,LA,BEAT,PARTI,SIGN,GIGGLE
BBOT_THETA_OPEN=0.50
BBOT_P50_OPEN=0.60
BBOT_MIN_PROFIT_PP=0.20
BBOT_MIN_THETA_CLOSE=0.05
BBOT_FEE_RT_PP=0.30
BBOT_FILL_DELAY_MS=70
BBOT_SLOT_K=1
```

### live canary (this contour)

```text
BBOT_MODE=policy
BBOT_PROFILE=gear22_live_canary
BBOT_THETA_TRADE=1
BBOT_THETA_LIVE_SEND=1
BBOT_BROKER=private_live
VENUE=live
LIVE_ORDERS=1
BBOT_NOTIONAL_USDT=10
BBOT_COINS=KAITO,HOME,WAL,RVN,ONT,2Z,BICO,HMSTR,CAP,BLEND,EDEN,KMNO,GPS,ME,ZBT,MOVE,COAI,AZTEC,APR,YB,ICX,AT,H,MUBARAK,ACU,LA,BEAT,PARTI,SIGN,GIGGLE
BBOT_THETA_OPEN=0.50
BBOT_P50_OPEN=0.60
BBOT_MIN_PROFIT_PP=0.20
BBOT_MIN_THETA_CLOSE=0.05
BBOT_FEE_RT_PP=0.30
BBOT_FILL_DELAY_MS=70
BBOT_SLOT_K=1
BBOT_DATA_ROOT=/data/bbot-gear22-live-canary
BBOT_LOG_PATH=/var/log/spread/bbot-gear22-live-canary.log
BBOT_PRIVATE_DATA_ROOT=/data/bbot-gear22-live-canary/private
BBOT_CHRONOMETRY=1
# BBOT_PRIVATE_SEND_PATH unset (Contour B trivial)
# keys, VENUE, LIVE_ORDERS, SENTRY_DSN only in mode-600 EnvironmentFile
```

Systemd example: [`deploy/systemd/spread-bbot-gear22-live-canary.service`](../deploy/systemd/spread-bbot-gear22-live-canary.service).  
Secrets file (not in git): `/etc/spread/bbot-gear22-live-canary.env` mode `600`.

## Sentry

Init **only** if `SENTRY_DSN` is set (secret env file). Rare events: open/close
success and real send errors/rejects. **Not** `insufficient_size` spam.

- Level ≈ `error`, fingerprint `["theta_k1", trade_id, event]`, `flush`.
- Tags: `environment`, `release`, `branch`, `contour=contour_b`.
- Suggested: `SENTRY_ENVIRONMENT=gear22-live-canary` and the Contour B project DSN.
- Optional: `SENTRY_RELEASE`, `SENTRY_BRANCH`.

## Compare `trade_id` across contours

Open UUID is shared:

| Contour | Path | Field |
|---------|------|--------|
| would_send | `/data/bbot-theta-k1-canary/theta_trades/event_date=YYYY-MM-DD/trades.jsonl` | `trade_id` |
| live theta | `/data/bbot-gear22-live-canary/theta_trades/event_date=YYYY-MM-DD/trades.jsonl` | `trade_id` **and** open `intent_id` (same UUID) |
| live wire / dual-leg | `{BBOT_PRIVATE_DATA_ROOT}/wire/` and chronometry `{BBOT_DATA_ROOT}/reports/trades/<intent_id>/` | `intent_id` = open `trade_id` |

Close is a second live `intent_id` (clOrdId must stay unique) journaled as
`close_intent_id` on the theta close row; `trade_id` still matches the open.

Join: same `trade_id` on both theta_trades trees. Then join live private
journal / wire on `intent_id`.

## Local tests

```bash
PYTHONPATH=. python3 -m unittest \
  tests.test_bbot_theta_live_canary tests.test_bbot_theta_trade_k1 \
  tests.test_trivial_dual_leg tests.test_sentry_integration -v
```
