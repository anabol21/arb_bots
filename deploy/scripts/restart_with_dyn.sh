#!/usr/bin/env bash
# VPS-only helper. NOT executed from this checkout and not a deploy step.
# Requires explicit live-campaign approval. Never SIGKILLs; never touches
# /data/bbot-would-send-prod/*.
#
# On the VPS as root:
#   bash restart_with_dyn.sh /tmp/gear23-pool-dyn-launch.sh dyn
# Flat-check -> SIGTERM old canary -> start new launcher detached -> write pid/last_launcher.
set -euo pipefail
LAUNCHER="${1:?launcher path}"; MODE="${2:-}"
RUNROOT=/root/b-private-b-exp/response-manager/20261005T194200Z-gear22-longrun
CODE=/root/b-private-b-exp/response-manager-code/gear23-b2.3-tw-deque-20261010-tradeobs-20261018T1741Z
LOG="$RUNROOT/bbot-gear23-b2.3.log"
PIDF="$RUNROOT/gear23-b2.3-twdeque.pid"
TS=$(date -u +%Y%m%dT%H%M%SZ)
[ -x "$LAUNCHER" ] || { echo "launcher not executable: $LAUNCHER"; exit 2; }
bash -n "$LAUNCHER"

# --- 0. inputs (own files under RUNROOT, never the prod would_send files)
if [ "$MODE" = dyn ]; then
  P="$RUNROOT/state/pool_dyn"
  test -s "$P/hot_add_delta_dyn.csv" && test -s "$P/hot_add_drop_dyn.csv"
  for c in API3 BLUR CT DOOD IOST MEW RSR SAND TRUST; do grep -q "^$c," "$P/hot_add_delta_dyn.csv" || { echo "seed coin $c missing in dyn delta"; exit 2; }; done
elif [ "$MODE" = pool12 ]; then
  P="$RUNROOT/state/pool12"
  test -s "$P/hot_add_delta_pool12.csv" && test -s "$P/hot_add_drop_pool12.csv"
fi

# --- 1. identify old process
OLD=$(cat "$PIDF")
[ "$(readlink /proc/$OLD/cwd)" = "$CODE" ] || { echo "pid $OLD cwd mismatch"; exit 2; }
ps -o args= -p "$OLD" | grep -q 'python -m app.bot' || { echo "pid $OLD not app.bot"; exit 2; }

# --- 2. flat check on the LIVE heartbeat, twice (>=6s apart), fresh <120s (startup flat-check of the new run covers only ITS coins!)
flatcheck() {
  HB=$(grep '| heartbeat |' "$LOG" | tail -1)
  if echo "$HB" | grep -q 'pending=False' && echo "$HB" | grep -q 'position=None'; then :; else echo "NOT FLAT: $(echo "$HB" | cut -c1-120) ... $(echo "$HB" | grep -o 'position=[^|]*' | cut -c1-120)"; exit 4; fi
  HB_TS=$(echo "$HB" | cut -c1-19); AGE=$(( $(date -u +%s) - $(date -u -d "$HB_TS" +%s) ))
  [ "$AGE" -lt 120 ] || { echo "heartbeat stale ${AGE}s"; exit 4; }
}
flatcheck; sleep 6; flatcheck
# no order wire_out within the last 60s (in-flight intent guard)
LASTORD=$(tail -n 20000 "$LOG" | grep -E 'wire_out .*op=order' | tail -1 | cut -c1-19 || true)
if [ -n "$LASTORD" ]; then OA=$(( $(date -u +%s) - $(date -u -d "$LASTORD" +%s) )); [ "$OA" -gt 60 ] || { echo "order wire_out ${OA}s ago; not restarting"; exit 4; }; fi
tail -n 20000 "$LOG" | grep -E 'canary29_cycle_flat|wire_out .*op=order' | tail -3 | cut -c1-160 || true
echo "flat OK x2 (hb age ${AGE}s)"

# --- 3. SIGTERM (handler saves floor_warm.pkl, sets stop_event). NOT SIGHUP (also = stop here).
kill -TERM "$OLD"
for i in $(seq 1 90); do kill -0 "$OLD" 2>/dev/null || break; sleep 1; done
if kill -0 "$OLD" 2>/dev/null; then echo "old pid $OLD still alive after 90s; STOP, decide manually"; exit 5; fi
echo "old $OLD exited"

# --- 4. start new (detached, PPID 1 like current run)
cp -p "$PIDF" "$PIDF.bak-$TS"; cp -p "$RUNROOT/state/last_launcher.txt" "$RUNROOT/state/last_launcher.txt.bak-$TS"
OUT="$RUNROOT/gear23-launch-$TS.stdout.log"
START_UTC=$(date -u '+%Y-%m-%d %H:%M:%S')
setsid nohup "$LAUNCHER" >"$OUT" 2>&1 < /dev/null &
NEW=$!
echo "$NEW" > "$PIDF"
cat > "$RUNROOT/state/last_launcher.txt" <<EOT
launcher=$LAUNCHER
code=$CODE
mode=patched_flat_no_resume${MODE:+_$MODE}
started_utc=$TS
rollback_launcher=/tmp/gear23-pool12-launch.sh (12 frozen) | /tmp/gear23-rollback77-launch.sh (77)
EOT
echo "new pid $NEW  start_utc='$START_UTC'  stdout=$OUT"
echo "next: bash deploy/scripts/postcheck_dyn.sh '$START_UTC' ${MODE:-rollback}"
