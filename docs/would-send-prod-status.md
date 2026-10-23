# Would-send prod status

Prod gear 2.2 **would_send** (stub / no live orders) with hot-add expand-only.

## Live contour (VPS)

| Field | Value |
|-------|--------|
| Unit | `spread-bbot-would-send-prod` |
| Data | `/data/bbot-would-send-prod` |
| History (RO warm) | `/data/bbot-would-send-prod-history` |
| Code tree | `/root/spread_bbot_would_send_prod` → tip **`a12d593`** (DROP-idempotent; content on main via #64 squash) |
| Profile | `gear22_would_send` / broker `stub` / `BBOT_HOT_ADD=1` / `BBOT_HOT_ADD_MAX_EXTRA=48` |
| Pool | base-29 + extras **CT, AEON, OPN, ARX, RECALL, LQTY** (=35) |
| Rotate | `spread-bbot-would-send-prod-rotate.timer` @ 10:00 Europe/Moscow (expand-only) |
| Stop-watch | **not** enabled |

Retired: `spread-bbot-theta-k1-canary` (stopped+disabled). Contour B / collector untouched.

## Deploy artifacts in this repo

- `deploy/systemd/spread-bbot-would-send-prod.service`
- `deploy/systemd/spread-bbot-would-send-prod-rotate.service` + `.timer`
- `deploy/scripts/bbot_would_send_prod_rotate.py` → install to `/usr/local/sbin/bbot_would_send_prod_rotate.py`

## Promotion note (2026-10-02)

# Prod would_send promotion — 2026-10-02 (~14:38 MSK)

## Goal
Promote expand-only / hot-add would_send contour to prod naming; keep **only** that would_send prod bot (retire the other canary).

## Two canaries identified
| Role | Unit | State before | Code | Hot-add |
|------|------|--------------|------|---------|
| **hot-add expand** («контур с добавлением монет») | `spread-bbot-theta-top10-canary` | stopped+disabled ~13:05 MSK after CT RT | `a12d593` | YES (`BBOT_HOT_ADD=1`, max_extra=48) |
| **fixed-pool would_send** | `spread-bbot-theta-k1-canary` | running since 2026-09-19 | `bfc5a29` | NO |
| Contour B live (untouched) | `spread-bbot-gear22-live-canary` | already disabled | — | live orders — **left alone** |

## Actions executed on VPS `root@38.180.94.108`
1. Backed up unit files + rotate/stop scripts → `/root/backups/would-send-prod-promotion-20261002_113751`
2. **Stop+disable** `spread-bbot-theta-k1-canary` (SIGTERM timed out → SIGKILL; reset-failed)
3. **mv** `/data/bbot-theta-top10-canary` → `/data/bbot-would-send-prod` (+ symlink old→new)
4. **mv** `/data/bbot-theta-top10-history` → `/data/bbot-would-send-prod-history` (+ symlink)
5. Code symlink `/root/spread_bbot_would_send_prod` → `/root/spread_bbot_theta_top10_canary` (**same binary `a12d593`**, not redeployed `main`/`426d689` — tip has DROP-idempotent fix not yet on origin/main)
6. New unit `spread-bbot-would-send-prod.service` (Description: *prod would_send hot-add expand-only; stub/no live orders*)
7. Rotate script retargeted to prod DATA/UNIT; new `spread-bbot-would-send-prod-rotate.timer` @ 10:00 Europe/Moscow **enabled**
8. **stop-watch NOT re-enabled** (would kill prod on next RT)
9. Old top10 unit/timers left disabled; Contour B untouched; collector untouched
10. k1 metrics-compact timer left enabled (archival compaction of `/data/bbot-theta-k1-canary` only)

## Final state (success criteria)
- **Exactly one** would_send prod: `spread-bbot-would-send-prod` **active/running** MainPID established ~11:38:23 UTC / **14:38 MSK**
- Other would_send canary (`theta-k1`): **stopped+disabled**
- Coin pool: **base-29 + 6 extras = 35** (extras: CT, AEON, OPN, ARX, RECALL, LQTY); drop rows=0
- Data root: `/data/bbot-would-send-prod`
- Profile: `gear22_would_send` / broker `stub` / `BBOT_HOT_ADD=1` expand-only

## Code note
- Prod runs **`a12d593`** (`fix(bot): make BBOT hot-add DROP idempotent…`) — same as prior B3 canary tip.
- `origin/main` tip was `929b7d3` (includes `426d689` hot-add feat #64) but **does not contain `a12d593`**. Redeploy main later only after cherry-pick/merge of DROP-idempotent fix.

## Not done / follow-ups
- Do not delete `/data/bbot-theta-k1-canary` without explicit ask (historical would_send journals).
- Optionally disable `spread-bbot-theta-k1-metrics-compact.timer` once archival compaction is finished.
- Old unit files remain on disk (disabled) for rollback; backups under `/root/backups/…`.


## Live snapshot (~16:10 MSK / 13:10Z 2026-10-02)

```
=== STATUS 2026-10-02T13:10:25Z ===
MainPID=1574931
NRestarts=0
ActiveState=active
SubState=running
FragmentPath=/etc/systemd/system/spread-bbot-would-send-prod.service
ActiveEnterTimestamp=Fri 2026-10-02 11:38:23 UTC
SHA=a12d59335cafcec53003ab84735e2af3d9eaca1a
COINS_HB=state/hot_add_state.json False hot_add_delta.csv True driver/last_heartbeat.json False
  7 /data/bbot-would-send-prod/hot_add_delta.csv
  1 /data/bbot-would-send-prod/hot_add_drop.csv
  8 total
base_coin,okx_symbol,bybit_symbol,okx_tick_size,okx_lot_size,okx_min_size,bybit_tick_size,bybit_qty_step,bybit_min_order_qty,bybit_min_notional_value,discovered_at_utc
CT,CT-USDT-SWAP,CTUSDT,0.0001,1,1,0.0001,1,1,5,2026-10-02T07:00:32Z
AEON,AEON-USDT-SWAP,AEONUSDT,0.00001,1,1,0.00001,10,10,5,2026-10-02T07:00:32Z
OPN,OPN-USDT-SWAP,OPNUSDT,1e-05,1.0,1.0,1e-05,1.0,1.0,5,2026-10-02T07:00:32Z
ARX,ARX-USDT-SWAP,ARXUSDT,0.0001,1.0,1.0,0.0001,1.0,1.0,5,2026-10-02T07:00:32Z
RECALL,RECALL-USDT-SWAP,RECALLUSDT,1e-05,1.0,1.0,1e-05,1.0,1.0,5,2026-10-02T07:00:32Z
LQTY,LQTY-USDT-SWAP,LQTYUSDT,0.0001,1.0,1.0,0.0001,0.1,0.1,5,2026-10-02T07:00:32Z
2026-10-02 13:10:08,054 | INFO | ws_subscribe_ok | coin=KMNO | exchange=okx | channel=books5 | generation=3
2026-10-02 13:10:25,679 | INFO | heartbeat | mode=policy | profile=gear22_would_send | coins=KAITO,HOME,WAL,RVN,ONT,2Z,BICO,HMSTR,CAP,BLEND,EDEN,KMNO,GPS,ME,ZBT,MOVE,COAI,AZTEC,APR,YB,AT,H,MUBARAK,ACU,LA,BEAT,PARTI,SIGN,GIGGLE,AEON,ARX,CT,LQTY,OPN,RECALL | data_root=/data/bbot-would-send-prod | accepted=664794 | sup_stale=71910 | sup_gen=0 | pending=None | position=None | probe_done=False
2026-10-02 13:10:25,679 | INFO | heartbeat | mode=policy | profile=gear22_would_send | coins=KAITO,HOME,WAL,RVN,ONT,2Z,BICO,HMSTR,CAP,BLEND,EDEN,KMNO,GPS,ME,ZBT,MOVE,COAI,AZTEC,APR,YB,AT,H,MUBARAK,ACU,LA,BEAT,PARTI,SIGN,GIGGLE,AEON,ARX,CT,LQTY,OPN,RECALL | data_root=/data/bbot-would-send-prod | accepted=664794 | sup_stale=71910 | sup_gen=0 | pending=None | position=None | probe_done=False
enabled
enabled
disabled
```
