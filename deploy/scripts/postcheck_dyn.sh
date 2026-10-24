#!/usr/bin/env bash
# READ-ONLY. VPS-only. Run ~2-3 min after start:
#   bash postcheck_dyn.sh '<START_UTC>'
# Expected pool = WAL,RVN,LA + all coins in state/pool_dyn/hot_add_delta_dyn.csv (cap 27 extras).
set -uo pipefail
S="${1:?start utc 'YYYY-mm-dd HH:MM:SS'}"
RUNROOT=/root/b-private-b-exp/response-manager/20261005T194200Z-gear22-longrun
LOG="$RUNROOT/bbot-gear23-b2.3.log"; PID=$(cat "$RUNROOT/gear23-b2.3-twdeque.pid")
DELTA="$RUNROOT/state/pool_dyn/hot_add_delta_dyn.csv"
EXTRAS=$(tail -n +2 "$DELTA" | cut -d, -f1 | head -27)
W=$(( 3 + $(echo "$EXTRAS" | grep -c .) ))
ALLOW="WAL,RVN,LA,$(echo "$EXTRAS" | paste -sd,)"
FIRST=$(awk -v s="$S" 'substr($0,1,19)>=s {print NR; exit}' "$LOG")
new() { [ -n "$FIRST" ] && tail -n +"$FIRST" "$LOG"; }
ok(){ echo "OK   $*"; }; bad(){ echo "FAIL $*"; FAIL=1; }; FAIL=0
kill -0 "$PID" 2>/dev/null && ok "pid $PID alive" || bad "pid $PID dead"
ps -o lstart=,args= -p "$PID"
L=$(new | grep '| bbot_start |' | tail -1)
C=$(echo "$L" | grep -o 'coins=[^|]*' | sed 's/coins=//; s/ *$//')
N=$(echo "$C" | tr ',' '\n' | grep -c .)
[ "$N" = "$W" ] && ok "bbot_start coins=$N (want $W)" || bad "bbot_start coins=$N (want $W): $C"
echo "pool: $C"
EX=$(echo "$C" | tr ',' '\n' | grep -vxF -f <(echo "$ALLOW" | tr ',' '\n') || true)
[ -z "$EX" ] && ok "no coins outside base+delta" || bad "extra coins: $EX"
new | grep -m1 'gear23_startup_delta_loaded' | cut -c1-240
new | grep 'gear23_hot_add_enabled' | grep -q 'pool_dyn' && ok "hot-add source = pool_dyn file" || { new | grep -m1 'gear23_hot_add_enabled' | cut -c1-240; bad "hot-add source not pool_dyn"; }
new | grep -m1 'canary29_startup_flat_confirmed' | cut -c1-120 || bad "no startup_flat_confirmed"
new | grep -m1 'private_warm_started' | grep -q 'ready=True' && ok "private_warm ready=True" || bad "private_warm not ready"
for v in "venue=okx | socket=private.*op=login" "venue=okx | socket=trade.*op=login" "venue=bybit | socket=private.*op=auth" "venue=bybit | socket=trade.*op=auth"; do
  new | grep 'wire_in' | grep -cE "$v" | grep -qv '^0$' && ok "wire_in $v" || bad "no wire_in $v"; done
new | grep -m1 'ws_tasks_started' | cut -c1-120
echo "leverage_one_confirmed (distinct coins): $(new | grep 'gear23_leverage_one_confirmed' | grep -o 'base_coin=[A-Z0-9]*' | sort -u | wc -l)  failed: $(new | grep -c 'gear23_leverage_one_failed')"
new | grep 'gear23_leverage_one_failed' | cut -c1-200 | head -5
HB=$(new | grep '| heartbeat |' | tail -1); echo "$HB" | sed -E 's/coins=[^|]*/coins=<..>/' | cut -c1-330
[ -n "$HB" ] && ok "heartbeat present" || bad "no heartbeat yet"
E=$(new | grep -cE '\| (ERROR|CRITICAL) \||Traceback'); echo "ERROR/Traceback lines since start: $E"
new | grep -E '\| (ERROR|CRITICAL) \||Traceback' | cut -d'|' -f3 | sort | uniq -c | head
exit $FAIL
