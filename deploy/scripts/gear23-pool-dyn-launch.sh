#!/usr/bin/env bash
# TEMPLATE for the gear23 pool-dyn live canary. Env only; secrets by reference.
# Do not execute from a developer checkout. VPS-only after explicit live-campaign
# approval. Never inline API keys, passphrases, or Sentry DSNs.
#
# Sources (mode 600, outside git — names only):
#   /root/b-private-b-exp/response-manager/20261005T114149Z-response/confirmed_1x.env
#   /etc/spread/bbot-gear22-live-canary.env
#   /etc/spread/bbot-private-live.env   (via BBOT_PRIVATE_ENV_FILE)
#
# Pool: BBOT_COINS=WAL,RVN,LA + append-only $RUNROOT/state/pool_dyn/hot_add_delta_dyn.csv
# (seed 9 extras; daily sidecar may grow extras up to BBOT_HOT_ADD_MAX_EXTRA=27, total 30).
set -euo pipefail
CODE="/root/b-private-b-exp/response-manager-code/gear23-b2.3-tw-deque-20261010-tradeobs-20261018T1741Z"
RUNROOT="/root/b-private-b-exp/response-manager/20261005T194200Z-gear22-longrun"
cd "$CODE"
set -a
source /root/b-private-b-exp/response-manager/20261005T114149Z-response/confirmed_1x.env
source /etc/spread/bbot-gear22-live-canary.env
set +a
export VENUE=live
export LIVE_ORDERS=1
export PYTHONPATH="$CODE:/root/spread_bbot_would_send_prod"
export BBOT_DATA_ROOT="$RUNROOT/data"
export BBOT_PRIVATE_DATA_ROOT="$RUNROOT/private"
export BBOT_LOG_PATH="$RUNROOT/bbot-gear23-b2.3.log"
export BBOT_PRIVATE_ENV_FILE="/etc/spread/bbot-private-live.env"
export BBOT_MODE=policy
export BBOT_PROFILE=gear22_live_canary
export BBOT_BROKER=private_live
# pool-dyn: base = WAL,RVN,LA (in BBOT_CONFIRMED_1X_COINS). Extras = OWN append-only delta file
# ($RUNROOT/state/pool_dyn/hot_add_delta_dyn.csv): starts with the 9 seed rows (API3,BLUR,CT,DOOD,IOST,
# MEW,RSR,SAND,TRUST) and is extended each morning (~10:10 MSK) by canary_pool_dyn_append.py with
# top-10-volatile coins not yet in the pool (never removes; total pool cap 30 => MAX_EXTRA=27).
# Leverage 1x set+verified by the bot for non-confirmed extras (hot-add path, unchanged).
export BBOT_COINS="WAL,RVN,LA"
export BBOT_THETA_TRADE=1
export BBOT_THETA_LIVE_SEND=1
export BBOT_THETA_EXECUTION=terminal_private
export BBOT_THETA_POLICY=gear22
export BBOT_NOTIONAL_USDT=10
export BBOT_TRADE_LAT_MS=100
export BBOT_FLOOR_WATCH=1
export BBOT_TW_P50_WATCH=1
export BBOT_TW_P50_1M_ENGINE=deque
export SPREAD_TICK_AGE_MAX_MS=10000
export SPREAD_TICK_SKEW_MAX_MS=10000
export BBOT_THETA_WATCH=1
export BBOT_FLOOR_WARM=1
export BBOT_FLOOR_WARM_PATH="$RUNROOT/data/state/floor_warm.pkl"
export BBOT_CANARY_MAX_CYCLES=0
unset BBOT_CANARY_OPEN_WINDOW_HOURS
export BBOT_CANARY_RESUME_MANIFEST=
export BBOT_FILL_DELAY_MS=70
export BBOT_SLOT_K=1
export BBOT_HOT_ADD=1
export BBOT_HOT_ADD_SET_LEVERAGE=1
DYN_DIR="$RUNROOT/state/pool_dyn"
export BBOT_HOT_ADD_DELTA="$DYN_DIR/hot_add_delta_dyn.csv"
export BBOT_HOT_ADD_DROP="$DYN_DIR/hot_add_drop_dyn.csv"  # not read by bot code; kept for symmetry
export BBOT_HOT_ADD_MAX_EXTRA=27   # hard cap: 3 base + 27 extras = 30 (sidecar enforces the same total cap)
export BBOT_HOT_ADD_POLL_SEC=30
export BBOT_HOT_ADD_WARM=1
export BBOT_HOT_ADD_HISTORY_ROOT=/data/bbot-would-send-prod-history
export BBOT_HOT_ADD_HISTORY_HOURS=12
export BBOT_HOT_ADD_HISTORY_MIN_SMA12=40
# fail-closed: own delta must exist, contain all 9 seed coins, no base duplicates, extras <= MAX_EXTRA
test -s "$BBOT_HOT_ADD_DELTA"
GOT=$(tail -n +2 "$BBOT_HOT_ADD_DELTA" | cut -d, -f1 | sort | tr '\n' ',')
for c in API3 BLUR CT DOOD IOST MEW RSR SAND TRUST; do
  case ",$GOT" in *",$c,"*) ;; *) echo "pool-dyn delta missing seed coin $c: $GOT" >&2; exit 3;; esac
done
for c in WAL RVN LA; do
  case ",$GOT" in *",$c,"*) echo "pool-dyn delta contains base coin $c" >&2; exit 3;; esac
done
N=$(tail -n +2 "$BBOT_HOT_ADD_DELTA" | grep -c .)
[ "$N" -le "$BBOT_HOT_ADD_MAX_EXTRA" ] || { echo "pool-dyn delta rows $N > cap $BBOT_HOT_ADD_MAX_EXTRA" >&2; exit 3; }
[ "$(echo "$GOT" | tr ',' '\n' | grep -c .)" = "$N" ] || { echo "pool-dyn delta has duplicate coins" >&2; exit 3; }
exec /root/venv/bin/python -m app.bot
