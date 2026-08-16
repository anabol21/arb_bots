#!/usr/bin/env bash
# L1 supervisor: enable current production collector (N=337 write) and run
# matched XRP ping for warmup 600s + steady 3600s after subscribe-ready.
# Do not touch compact/backup units. Do not change ingest.
set -euo pipefail

RUN_ID="${1:-l1_n337_write_20260816}"
ROOT="${2:-/data/experiments/l1_n337_write_20260816}"
CODE_ROOT="${3:-/root/spread_staging}"
PYTHON="${PYTHON:-/root/venv/bin/python}"
PING_DURATION_SEC="${PING_DURATION_SEC:-4200}"
SUBSCRIBE_OK_MIN="${SUBSCRIBE_OK_MIN:-1000}"
SUBSCRIBE_WAIT_MAX_SEC="${SUBSCRIBE_WAIT_MAX_SEC:-900}"
RUNTIME_LOG="${RUNTIME_LOG:-/var/log/spread/runtime.log}"

mkdir -p "$ROOT"
status="$ROOT/supervisor.status"
marker="$ROOT/DO_NOT_TOUCH.md"
ping_log="$ROOT/ping_xrp.jsonl"
manifest="$ROOT/run_manifest.env"

utc_now() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }

log() {
  printf '%s %s\n' "$(utc_now)" "$*" | tee -a "$status"
}

cat >"$marker" <<EOF
DO_NOT_TOUCH: active Track (D) latency L1 profile run
run_id=$RUN_ID
owner=Track (D) latency / Validation
allowed_paths=$ROOT and /var/log/spread/DO_NOT_TOUCH_LATENCY_${RUN_ID}.txt
prohibited=kill/restart spread-collector; truncate runtime.log; delete/compact this experiment root; stop compact/backup timers (they are part of L1)
EOF
cp "$marker" "/var/log/spread/DO_NOT_TOUCH_LATENCY_${RUN_ID}.txt"

{
  echo "run_id=$RUN_ID"
  echo "root=$ROOT"
  echo "code_root=$CODE_ROOT"
  echo "ping_duration_sec=$PING_DURATION_SEC"
  echo "subscribe_ok_min=$SUBSCRIBE_OK_MIN"
  echo "started_utc=$(utc_now)"
} >"$manifest"

log "supervisor_start run_id=$RUN_ID"

if [[ "${SKIP_COLLECTOR_START:-0}" == "1" ]]; then
  if ! systemctl is-active --quiet spread-collector.service; then
    log "abort collector_inactive_skip_start"
    exit 2
  fi
  log "collector_already_active_reuse"
else
  if systemctl is-active --quiet spread-collector.service; then
    log "abort collector_already_active"
    exit 2
  fi
  systemctl enable --now spread-collector.service
  log "collector_enabled_now"
fi

for _ in $(seq 1 30); do
  if systemctl is-active --quiet spread-collector.service; then
    break
  fi
  sleep 1
done

if ! systemctl is-active --quiet spread-collector.service; then
  log "abort collector_failed_to_start"
  systemctl --no-pager --full status spread-collector.service || true
  exit 3
fi

collector_pid="$(systemctl show spread-collector.service -p MainPID --value)"
log "collector_active pid=$collector_pid"
# Wait for THIS process to emit connect-scheduler. The shared log still has
# previous canary/L1 lines; tail -1 of history is the wrong cut.
start_line=""
sched_deadline=$((SECONDS + SUBSCRIBE_WAIT_MAX_SEC))
while (( SECONDS < sched_deadline )); do
  start_line="$(awk -v pid="$collector_pid" '
    index($0, "Starting subscriptions via connect scheduler") { line=NR }
    END { print line }
  ' "$RUNTIME_LOG")"
  # Prefer a scheduler line that appears after this PID is mentioned, else
  # the newest scheduler line written after we started waiting.
  if [[ -n "$start_line" ]]; then
    after_pid="$(awk -v pid="$collector_pid" '
      $0 ~ pid { seen=1 }
      seen && index($0, "Starting subscriptions via connect scheduler") { line=NR }
      END { print line }
    ' "$RUNTIME_LOG")"
    if [[ -n "$after_pid" ]]; then
      start_line="$after_pid"
      break
    fi
  fi
  log "waiting_scheduler_line pid=$collector_pid"
  sleep 2
done
if [[ -z "$start_line" ]]; then
  log "abort no_connect_scheduler_line pid=$collector_pid"
  exit 4
fi
log "log_cut_line=$start_line"
echo "log_cut_line=$start_line" >>"$manifest"

subscribe_ready=""
deadline=$((SECONDS + SUBSCRIBE_WAIT_MAX_SEC))
while (( SECONDS < deadline )); do
  last_hb="$(awk -v s="$start_line" 'NR>=s && index($0, "heartbeat |") { line=$0 } END { print line }' "$RUNTIME_LOG")"
  ok="$(printf '%s\n' "$last_hb" | sed -n 's/.*ws_subscribe_ok=\([0-9][0-9]*\).*/\1/p')"
  pairs="$(printf '%s\n' "$last_hb" | sed -n 's/.*pairs=\([0-9][0-9]*\).*/\1/p')"
  if [[ -n "$ok" && "$ok" -ge "$SUBSCRIBE_OK_MIN" ]]; then
    subscribe_ready="$(utc_now)"
    log "subscribe_ready utc=$subscribe_ready ws_subscribe_ok=$ok pairs=${pairs:-?}"
    break
  fi
  log "waiting_subscribe ws_subscribe_ok=${ok:-na} pairs=${pairs:-na}"
  sleep 15
done

if [[ -z "$subscribe_ready" ]]; then
  log "abort subscribe_timeout waited_sec=$SUBSCRIBE_WAIT_MAX_SEC"
  exit 4
fi

echo "subscribe_ready_utc=$subscribe_ready" >>"$manifest"
echo "collector_pid=$collector_pid" >>"$manifest"

log "ping_start duration_sec=$PING_DURATION_SEC log=$ping_log"
set +e
"$PYTHON" "$CODE_ROOT/validation/ws_fanout_matched_ping.py" \
  --duration-sec "$PING_DURATION_SEC" \
  --log-file "$ping_log"
ping_rc=$?
set -e
log "ping_exit rc=$ping_rc"

{
  echo "ping_exit=$ping_rc"
  echo "finished_utc=$(utc_now)"
  echo "collector_active=$(systemctl is-active spread-collector.service || true)"
  echo "collector_pid_end=$(systemctl show spread-collector.service -p MainPID --value)"
  echo "nrestarts=$(systemctl show spread-collector.service -p NRestarts --value)"
} >>"$manifest"

if [[ "$ping_rc" -eq 0 ]]; then
  log "supervisor_finished"
else
  log "supervisor_finished_with_ping_error rc=$ping_rc"
fi
exit "$ping_rc"
