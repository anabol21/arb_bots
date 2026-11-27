# Gear 2.3 canary pool-dyn (append-only expansion)

Contour B live canary (gear23 / B2.3) no longer treats the 12-coin set as a
frozen pool. Those 12 coins are the **starting base**. A canary-owned sidecar
appends new morning top-10 volatile coins that are not already in the pool.
Nothing is removed. Trading thresholds, filters, and the bot hot-add path are
unchanged.

This document is the operator contract. It does not authorize a live restart or
a deploy.

## Why a sidecar

Until 2026-10-22 the canary read the strategist-owned shared file
`/data/bbot-would-send-prod/hot_add_delta.csv` (rotated daily at 10:00 MSK by
`spread-bbot-would-send-prod-rotate.timer` → `bbot_top10_canary_rotate.py` /
the in-tree `deploy/scripts/bbot_would_send_prod_rotate.py`). On 22.10 the
canary was pointed at a frozen 9-row copy
(`state/pool12/hot_add_delta_pool12.csv`, `BBOT_HOT_ADD_MAX_EXTRA=9`) so new
would_send extras stopped arriving.

The shared file stays owned by the strategist. This job never writes it and
never changes its rotation.

## Daily flow (MSK)

1. **09:10** `spread-top10-volatile-spread.timer` runs
   `render_top10_volatile_spread.py` (VPS ops script; ranks by
   `std(spread_long_bps)` OKX−Bybit over 24h) and writes
   `/var/log/spread/top10_volatile_spread.json` with a `top10` list.
2. **10:00** shared would_send rotate appends missing top-10 coins to
   `/data/bbot-would-send-prod/hot_add_delta.csv` (expand-only; skip that
   rotator's own BASE such as BLEND/APR). Do not touch this file.
3. **10:10** `spread-canary-pool-dyn-append.timer` runs
   `app/bot/canary_pool_dyn_append.py --apply` (installed copy:
   `/root/pool12/dyn/canary_pool_dyn_append.py`). It appends top-10 coins that
   are not yet in **this** contour's pool.

`render_top10_volatile_spread.py` is not in this git tree. Parsing matches the
would_send rotator already in-tree: `data["top10"][*]["base_coin"]`, plus
`stamp` / `window_end_msk` for the same-day check. A list of strings is also
accepted.

## Pool rules

| Piece | Value |
|---|---|
| Base (`BBOT_COINS`) | WAL, RVN, LA — always kept, never listed in the delta |
| Seed extras (never-remove) | API3, BLUR, CT, DOOD, IOST, MEW, RSR, SAND, TRUST |
| Own delta | `$RUNROOT/state/pool_dyn/hot_add_delta_dyn.csv` |
| Total cap | 30 (configurable `--max-total` / `POOL_DYN_MAX_TOTAL`) |
| Bot extras cap | `BBOT_HOT_ADD_MAX_EXTRA=27` (30 − 3 base) |
| Dedup | skip if already in base or own delta |
| would_send BASE | **not** skipped (BLEND, APR, … may be added) |
| Metadata | first complete lot/tick row from shared delta, shared universe, code-tree universe, or collector discovery delta. No REST. |
| Skip | incomplete metadata, ticker ≠ `{COIN}-USDT-SWAP` / `{COIN}USDT`, non-crypto, cap overflow |
| Stale digest | `stamp` / `window_end_msk` not today's MSK date → exit 3, no write |
| Launcher gate | `--apply` writes only when `state/last_launcher.txt` contains `gear23-pool-dyn-launch` (or `--force`) |
| Default | dry-run; `--apply` is required to write |

Idempotent: a second run with the same digest is a no-op. Atomic write is
tmp + fsync + rename, with a timestamped `.bak-*` of the previous bytes.
The previous file contents remain a prefix of the new file (append-only).

## Optional std guard (default off)

Morning top-10 can include coins with huge 24h spread std (examples from the
2026-10 window: IRYS, AXIS, BTW > 95 bps; AXIS/BTW mean > 110 bps). Ticker
mismatch across venues is already skipped (exact OKX/Bybit symbols required).

A **separate, default-off** sanity check can skip a candidate whose digest
`std_*_bps` exceeds `--max-std-bps` / `POOL_DYN_MAX_STD_BPS`. When the guard
is on and the digest row has no std field, the coin is skipped (`std_missing`).
Do not enable this on the VPS unit unless an operator explicitly chooses a
threshold. It is not a change to trading filters.

## Runtime: no bot-code change

The existing Gear 2.3 hot-add poller already:

- reads the whole cumulative snapshot every `BBOT_HOT_ADD_POLL_SEC` (30s);
- caps extras with `BBOT_HOT_ADD_MAX_EXTRA`;
- requires complete lot/tick, crypto-yes, public/private readiness, history
  warm (`BBOT_HOT_ADD_WARM=1`), and confirmed 1x for extras
  (`BBOT_HOT_ADD_SET_LEVERAGE=1`).
- requires `BBOT_CONFIRMED_1X_COINS` only for the **base/held** coins, not for
  newly hot-added extras.

Raising `BBOT_HOT_ADD_MAX_EXTRA` from 9 to 27 in the launcher is enough for
the pool to grow at runtime after the sidecar appends a row. Policy knobs are
not touched.

## Deploy (VPS; not done from this PR)

Paths stay compatible with the host copy under `/root/pool12/dyn` and
`$RUNROOT/state/pool_dyn/*`.

1. Seed the own delta **once** (if not already present) from the frozen 12-pool
   file. Do not invent metadata:
   `cp $RUNROOT/state/pool12/hot_add_delta_pool12.csv $RUNROOT/state/pool_dyn/hot_add_delta_dyn.csv`
   and keep a header-only `hot_add_drop_dyn.csv`.
2. `cp app/bot/canary_pool_dyn_append.py /root/pool12/dyn/canary_pool_dyn_append.py`
3. Install `deploy/systemd/spread-canary-pool-dyn-append.{service,timer}`.
4. Copy `deploy/scripts/gear23-pool-dyn-launch.sh` to the VPS launch path
   (typically `/tmp/gear23-pool-dyn-launch.sh`). Secrets stay in the sourced
   env files; nothing is inlined.
5. Only after an explicit live-campaign approval, and only when flat: restart
   onto that launcher (`deploy/scripts/restart_with_dyn.sh`). The sidecar
   `--apply` path no-ops until `last_launcher.txt` contains
   `gear23-pool-dyn-launch`.
6. Enable the 10:10 timer after the dyn launcher is the running one.
7. Read-only check: `deploy/scripts/postcheck_dyn.sh '<START_UTC>'`.

The in-tree timer is `Persistent=false` (same as the VPS unit): a missed 10:10
does not catch up. The next successful morning still appends that day's new
top-10 names.

## Rollback

- **Stop expansion, keep current extras:** disable
  `spread-canary-pool-dyn-append.timer`. The bot keeps polling the last own
  delta; nothing is removed.
- **Back to frozen 12:** restart onto `/tmp/gear23-pool12-launch.sh`
  (`BBOT_HOT_ADD_DELTA` → `state/pool12/hot_add_delta_pool12.csv`,
  `MAX_EXTRA=9`). Own dyn CSV is left in place for a later return.
- **Back to 77 / shared file:** `/tmp/gear23-rollback77-launch.sh` (unchanged;
  still must not rewrite the shared would_send file).

## Local check

```bash
python3 -m unittest tests.test_canary_pool_dyn_append
```

Unverified from this checkout (no VPS access): the live PID, the exact
`top10_volatile_spread.json` field names beyond `top10`/`base_coin`/`stamp`/
`window_end_msk`, whether `/root/pool12/dyn` already has the sidecar copy,
and whether `state/pool_dyn/*` is already seeded.
