#!/usr/bin/env bash
# OWNERSHIP / ВЛАДЕНИЕ: Track (D) latency dose-N, run dose_n50_prod337_20260811d.
# DO NOT TOUCH / НЕ ТРОГАТЬ: only the owning Runtime/Validation agent may alter
# this wrapper, its unit, PIDs, logs, or experiment root until EXPECTED_END_UTC.
# Other Track (D) work may read only: no kill/restart/truncate/rotate/delete/compact/reclaim.
# This launches one isolated N=50 XRP-including shadow plus one fresh XRP matched ping;
# production N≈337 is read-only evidence and is never stopped, restarted, or reconfigured.
set -euo pipefail

RUN_ID="${RUN_ID:-dose_n50_prod337_20260811d}"
DURATION_SEC="${DURATION_SEC:-1800}"
CODE_ROOT="${CODE_ROOT:-/root/spread_staging}"
PY="${PY:-/root/spread_venv/bin/python}"
LOG_DIR="${LOG_DIR:-/var/log/spread}"
EXP_ROOT="${EXP_ROOT:-/data/experiments}"
PROD_UNIT="${PROD_UNIT:-spread-collector}"
UNIT_NAME="${UNIT_NAME:-dose-n50-prod337-20260811d.service}"
ROOT="${EXP_ROOT}/${RUN_ID}"
RUNTIME_LOG="${LOG_DIR}/${RUN_ID}_runtime.log"
FAILED_LOG="${LOG_DIR}/${RUN_ID}_failed_batches.log"
PING_LOG="${LOG_DIR}/${RUN_ID}_ping_dual.log"
SUPERVISOR_LOG="${LOG_DIR}/${RUN_ID}_supervisor.log"
STATUS_FILE="${LOG_DIR}/${RUN_ID}.status"
OWNERSHIP_FILE="${LOG_DIR}/DO_NOT_TOUCH_LATENCY_DOSE_20260811D.txt"
SHADOW_PID_FILE="${LOG_DIR}/${RUN_ID}_shadow.pid"
PING_PID_FILE="${LOG_DIR}/${RUN_ID}_ping.pid"
SUPERVISOR_PID_FILE="${LOG_DIR}/${RUN_ID}_supervisor.pid"
META_FILE="${ROOT}/arm_meta.env"
ROW_START=280
ROW_END=330

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
utc_after() { date -u -d "@$(( $(date +%s) + $1 ))" +%Y-%m-%dT%H:%M:%SZ; }
write_status() {
  {
    printf 'updated_utc=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf 'run_id=%s\nphase=%s\ndetail=%s\n' "$RUN_ID" "$1" "$2"
  } >"$STATUS_FILE"
}
pid_is_running() { kill -0 "$1" 2>/dev/null; }

mkdir -p "$ROOT/live" "$ROOT/spool" "$LOG_DIR"
exec >>"$SUPERVISOR_LOG" 2>&1
trap '' HUP
printf '%s\n' "$$" >"$SUPERVISOR_PID_FILE"

if ! systemctl is-active --quiet "$PROD_UNIT"; then
  log "ERROR production unit inactive; refusing launch"
  write_status "blocked" "production_inactive"
  exit 2
fi
PROD_PID="$(systemctl show -p MainPID --value "$PROD_UNIT")"
if [[ ! "$PROD_PID" =~ ^[1-9][0-9]*$ ]] || ! pid_is_running "$PROD_PID"; then
  log "ERROR invalid production PID=${PROD_PID}; refusing launch"
  write_status "blocked" "invalid_production_pid"
  exit 2
fi
CHRONY="$(timeout 15s chronyc tracking || true)"
if ! grep -q 'Leap status.*Normal' <<<"$CHRONY"; then
  log "ERROR chrony not synchronized: ${CHRONY//$'\n'/ | }"
  write_status "blocked" "chrony_not_normal"
  exit 2
fi
PROD_XRP="$(python3 - <<'PY'
from pathlib import Path
files = [p for p in Path("/data/live/base_coin=XRP").rglob("*.parquet") if p.is_file()]
if files:
    print(max(files, key=lambda p: p.stat().st_mtime))
PY
)"
if [[ -z "$PROD_XRP" || ! -r "$PROD_XRP" ]]; then
  log "ERROR no readable production XRP parquet; refusing launch"
  write_status "blocked" "no_readable_production_xrp"
  exit 2
fi
PROD_HEARTBEAT="$(grep -E 'heartbeat.*pairs=|Loaded pairs:' /var/log/spread/runtime.log | tail -1 || true)"
EXPECTED_END_UTC="$(utc_after $((DURATION_SEC + 300)))"
cat >"$OWNERSHIP_FILE" <<EOF
DOSE_N_LATENCY_OWNED
owner=Track_(D)_latency_Runtime_and_Validation
status=running
run_id=${RUN_ID}
host=$(hostname)
unit=${UNIT_NAME}
supervisor_pid=$$
production_pid=${PROD_PID}
start_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)
expected_end_utc=${EXPECTED_END_UTC}
paths=${ROOT};${RUNTIME_LOG};${PING_LOG};${SUPERVISOR_LOG}
RU=Другим веткам Track_(D) разрешено только чтение: не kill/restart/truncate/rotate/delete/compact/reclaim shadow, PID, ping, logs или root до expected_end_utc.
EOF
cat >"$ROOT/DO_NOT_TOUCH.md" <<EOF
# DO NOT TOUCH — ${RUN_ID}

Owner: Track (D) latency / Runtime+Validation
Unit: ${UNIT_NAME}
Supervisor PID: $$
Production PID (read-only): ${PROD_PID}
Expected ownership end UTC: ${EXPECTED_END_UTC}

**Другим веткам Track (D): только чтение. Не kill/restart/truncate/rotate/delete/compact/reclaim**
shadow, PID-файлы, ping, логи или этот root до указанного времени.
EOF
{
  printf 'run_id=%s\nrow_start=%s\nrow_end=%s\npairs_expected=50\n' "$RUN_ID" "$ROW_START" "$ROW_END"
  printf 'collect_bars=0\npersist_every=5000\nproduction_pid=%s\nproduction_xrp=%s\n' "$PROD_PID" "$PROD_XRP"
  printf 'chrony=%q\nproduction_heartbeat=%q\nexpected_end_utc=%s\n' "$CHRONY" "$PROD_HEARTBEAT" "$EXPECTED_END_UTC"
} >"$META_FILE"
log "chrony=${CHRONY//$'\n'/ | }"
log "production pid=${PROD_PID} xrp=${PROD_XRP} heartbeat=${PROD_HEARTBEAT}"

cd "$CODE_ROOT"
setsid env \
  SPREAD_ROW_START="$ROW_START" SPREAD_ROW_END="$ROW_END" \
  SPREAD_COLLECT_BARS=0 SPREAD_LEAN_SCHEMA=1 SPREAD_PERSIST_EVERY=5000 \
  SPREAD_PARQUET_ROOT="$ROOT/live" SPREAD_SPOOL_ROOT="$ROOT/spool" \
  SPREAD_RUNTIME_LOG="$RUNTIME_LOG" SPREAD_FAILED_BATCHES_LOG="$FAILED_LOG" \
  "$PY" app/screaner_b_o.py >"${LOG_DIR}/${RUN_ID}_shadow.nohup.out" 2>&1 </dev/null &
SHADOW_PID=$!
printf '%s\n' "$SHADOW_PID" >"$SHADOW_PID_FILE"
write_status "shadow_starting" "shadow_pid=${SHADOW_PID} production_pid=${PROD_PID}"
log "shadow_pid=${SHADOW_PID}"

for _ in $(seq 1 48); do
  if grep -q 'Loaded pairs: 50' "$RUNTIME_LOG" 2>/dev/null \
    && grep -q 'XRP | OKX subscribed' "$RUNTIME_LOG" 2>/dev/null \
    && grep -q 'XRP | Bybit subscribed' "$RUNTIME_LOG" 2>/dev/null; then
    break
  fi
  if ! pid_is_running "$SHADOW_PID"; then
    log "ERROR shadow exited before smoke"
    write_status "failed" "shadow_exited_before_smoke"
    exit 3
  fi
  sleep 5
done
if ! grep -q 'Loaded pairs: 50' "$RUNTIME_LOG" \
  || ! grep -q 'XRP | OKX subscribed' "$RUNTIME_LOG" \
  || ! grep -q 'XRP | Bybit subscribed' "$RUNTIME_LOG"; then
  log "ERROR shadow smoke failed"
  write_status "failed" "shadow_smoke_failed"
  kill -TERM "$SHADOW_PID" 2>/dev/null || true
  exit 3
fi
log "shadow smoke passed: $(grep -E 'Loaded pairs: 50|XRP \| (OKX|Bybit) subscribed' "$RUNTIME_LOG" | tail -3 | tr '\n' ' | ')"

setsid "$PY" validation/ping_okx_bybit_2h.py \
  --duration-sec "$DURATION_SEC" --okx-inst XRP-USDT-SWAP --bybit-symbol XRPUSDT \
  --log-file "$PING_LOG" >"${LOG_DIR}/${RUN_ID}_ping.nohup.out" 2>&1 </dev/null &
PING_PID=$!
printf '%s\n' "$PING_PID" >"$PING_PID_FILE"
PING_START_UTC="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
PING_END_UTC="$(utc_after "$DURATION_SEC")"
{
  printf 'shadow_pid=%s\nping_pid=%s\nping_start_utc=%s\nping_expected_end_utc=%s\n' \
    "$SHADOW_PID" "$PING_PID" "$PING_START_UTC" "$PING_END_UTC"
} >>"$META_FILE"
cat >>"$ROOT/DO_NOT_TOUCH.md" <<EOF
Shadow PID: ${SHADOW_PID}
Ping PID: ${PING_PID}
Ping start UTC: ${PING_START_UTC}
Ping expected end UTC: ${PING_END_UTC}
EOF
write_status "running" "shadow_pid=${SHADOW_PID} ping_pid=${PING_PID} ping_end=${PING_END_UTC} production_pid=${PROD_PID}"
log "ping_pid=${PING_PID} ping_start=${PING_START_UTC} ping_expected_end=${PING_END_UTC}"

wait "$PING_PID" || {
  log "ERROR ping failed"
  write_status "failed" "ping_failed"
  kill -TERM "$SHADOW_PID" 2>/dev/null || true
  exit 4
}
log "ping finished; terminating shadow pid=${SHADOW_PID}"
kill -TERM "$SHADOW_PID" 2>/dev/null || true
for _ in $(seq 1 180); do
  pid_is_running "$SHADOW_PID" || break
  sleep 1
done
if pid_is_running "$SHADOW_PID"; then
  log "ERROR shadow did not terminate gracefully"
  write_status "failed" "shadow_shutdown_timeout"
  exit 5
fi
printf 'ping_end_utc=%s\nstatus=complete\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >>"$META_FILE"
write_status "complete" "ping_finished_shadow_shutdown"
log "complete"
