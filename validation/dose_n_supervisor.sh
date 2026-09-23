#!/usr/bin/env bash
# OWNERSHIP / ВЛАДЕНИЕ: Track (D) latency dose-N, серия ${SERIES_DATE:-20260811c}.
# DO NOT TOUCH / НЕ ТРОГАТЬ: этот wrapper, unit, PID-файлы, логи и experiment roots
# принадлежат только этому эксперименту до завершения серии. Другие Track (D) задачи
# могут только читать: не kill/restart, не truncate/rotate и не delete/compact/reclaim.
# E6-lite: sequential N=3,10 shadow+ping, then production N≈337 XRP∩ping.
# Host: NEW only. Does NOT stop prod. Does NOT launch a second full-N shadow.
set -euo pipefail

SERIES_DATE="${SERIES_DATE:-20260811c}"
DURATION_SEC="${DURATION_SEC:-1500}"
STEADY_DROP_SEC="${STEADY_DROP_SEC:-300}"
CODE_ROOT="${CODE_ROOT:-/root/spread_staging}"
PY="${PY:-/root/spread_venv/bin/python}"
LOG_DIR="${LOG_DIR:-/var/log/spread}"
EXP_ROOT="${EXP_ROOT:-/data/experiments}"
SUPERVISOR_UNIT="${SUPERVISOR_UNIT:-dose-n-supervisor-${SERIES_DATE}.service}"
STATUS_FILE="${STATUS_FILE:-${LOG_DIR}/dose_n_supervisor_${SERIES_DATE}.status}"
SUPER_LOG="${SUPER_LOG:-${LOG_DIR}/dose_n_supervisor_${SERIES_DATE}.log}"
OWNED="${OWNED:-${LOG_DIR}/DO_NOT_TOUCH_LATENCY_DOSE_${SERIES_DATE^^}.txt}"
# Detach children from SSH/session so SIGHUP/stray group signals do not abort arms.
LAUNCH=(setsid)

SHADOW_NS=(3 10)
END_ROW=330
XRP_INDEX=329
SERIES_START_UTC="${SERIES_START_UTC:-$(date -u +%Y-%m-%dT%H:%M:%SZ)}"
SERIES_END_UTC="${SERIES_END_UTC:-$(date -u -d "@$(( $(date +%s) + (3 * DURATION_SEC) + 900 ))" +%Y-%m-%dT%H:%M:%SZ)}"

mkdir -p "$LOG_DIR" "$EXP_ROOT"
exec >>"$SUPER_LOG" 2>&1

log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $*"; }
set_status() {
  {
    echo "updated_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "series_date=${SERIES_DATE}"
    echo "supervisor_unit=${SUPERVISOR_UNIT}"
    echo "phase=$1"
    echo "detail=$2"
    echo "duration_sec=${DURATION_SEC}"
  } >"$STATUS_FILE"
}

write_owned() {
  cat >"$OWNED" <<EOF
DOSE_N_LATENCY_OWNED
owner=Track_(D)_latency_dose_response
status=running
run_id=dose_n_${SERIES_DATE}
host=$(hostname)
start_utc=${SERIES_START_UTC}
expected_end_utc=${SERIES_END_UTC}
service_unit=${SUPERVISOR_UNIT}
pid_file=${LOG_DIR}/dose_n_supervisor_${SERIES_DATE}.pid
DO_NOT_TOUCH=/data/experiments/dose_n{3,10,337_prod}_${SERIES_DATE}/
DO_NOT_TRUNCATE=${LOG_DIR}/dose_n*_${SERIES_DATE}_*
RU=Другим задачам Track_(D): только чтение; не kill/restart/truncate/rotate/delete/compact до expected_end_utc.
EN=Other Track (D) work is read-only: do not kill, restart, truncate, rotate, delete, compact, or reclaim until expected_end_utc.
note=sequential_E6_lite; production remains untouched; no_second_full_N_shadow
EOF
}

write_arm_marker() {
  local root="$1" run_id="$2" n_label="$3"
  cat >"${root}/DO_NOT_TOUCH.md" <<EOF
# DO_NOT_TOUCH — ${run_id}

## ВЛАДЕЛЕЦ / OWNER

Owner: Track (D) latency dose-response / E6-lite
Run ID: ${run_id}
N label: ${n_label}
Series: ${SERIES_DATE}
Host: $(hostname)
Start UTC: $(date -u +%Y-%m-%dT%H:%M:%SZ)
Expected series end UTC: ${SERIES_END_UTC}
Service unit: ${SUPERVISOR_UNIT}
PID files: ${LOG_DIR}/${run_id}_shadow.pid ; ${LOG_DIR}/${run_id}_ping.pid

**НЕ ТРОГАТЬ / DO NOT TOUCH:** do not kill/restart associated processes, truncate/rotate logs,
delete/compact/reclaim this tree, or change its service until the series completes. Other Track (D)
work may only read these artifacts. Validation/Orchestrator approval is required for any mutation.
EOF
}

wait_pid_gone() {
  local pid="$1" timeout_sec="${2:-180}" i=0
  while kill -0 "$pid" 2>/dev/null; do
    i=$((i + 1))
    if [[ $i -ge $timeout_sec ]]; then
      log "WARN pid ${pid} still alive after ${timeout_sec}s"
      return 1
    fi
    sleep 1
  done
  return 0
}

verify_chrony() {
  if ! systemctl is-active --quiet chrony; then
    log "ERROR chrony inactive"
    exit 2
  fi
  local leap
  leap=$(chronyc tracking 2>/dev/null | awk -F': ' '/Leap status/ {print $2}')
  log "chrony leap=${leap:-unknown}"
  chronyc tracking 2>/dev/null | egrep 'System time|Last offset|Leap status|Stratum' || true
}

smoke_shadow() {
  local runtime_log="$1" expect_n="$2" tries=36
  local i=0
  while [[ $i -lt $tries ]]; do
    if grep -q "Loaded pairs: ${expect_n}" "$runtime_log" 2>/dev/null; then
      log "smoke OK Loaded pairs: ${expect_n}"
      grep -E 'Loaded pairs|XRP \| (OKX|Bybit) subscribed|runtime_paths|schema_mode' "$runtime_log" | head -20 || true
      return 0
    fi
    i=$((i + 1))
    sleep 5
  done
  log "ERROR smoke failed for Loaded pairs: ${expect_n}"
  tail -50 "$runtime_log" || true
  return 1
}

run_shadow_arm() {
  local n="$1"
  local start=$((END_ROW - n))
  local run_id="dose_n${n}_${SERIES_DATE}"
  local root="${EXP_ROOT}/${run_id}"
  local runtime_log="${LOG_DIR}/${run_id}_runtime.log"
  local failed_log="${LOG_DIR}/${run_id}_failed_batches.log"
  local ping_log="${LOG_DIR}/${run_id}_ping_dual.log"
  local shadow_pid_file="${LOG_DIR}/${run_id}_shadow.pid"
  local ping_pid_file="${LOG_DIR}/${run_id}_ping.pid"
  local meta="${root}/arm_meta.env"
  local expected_end_utc

  mkdir -p "${root}/live" "${root}/spool"
  write_arm_marker "$root" "$run_id" "$n"
  : >"$runtime_log"
  : >"$failed_log"
  : >"$ping_log"

  set_status "shadow_n${n}" "starting"
  log "=== ARM N=${n} START=${start} END=${END_ROW} run_id=${run_id} ==="
  log "prod_active=$(systemctl is-active spread-collector || true)"

  cd "$CODE_ROOT"
  # setsid: new session so SSH hangup / process-group TERM cannot abort the arm mid-window
  "${LAUNCH[@]}" env \
    SPREAD_ROW_START="$start" \
    SPREAD_ROW_END="$END_ROW" \
    SPREAD_COLLECT_BARS=0 \
    SPREAD_LEAN_SCHEMA=1 \
    SPREAD_PERSIST_EVERY=5000 \
    SPREAD_PARQUET_ROOT="${root}/live" \
    SPREAD_SPOOL_ROOT="${root}/spool" \
    SPREAD_RUNTIME_LOG="$runtime_log" \
    SPREAD_FAILED_BATCHES_LOG="$failed_log" \
    "$PY" app/screaner_b_o.py \
    >"${LOG_DIR}/${run_id}_shadow.nohup.out" 2>&1 </dev/null &
  local shadow_pid=$!
  echo "$shadow_pid" >"$shadow_pid_file"
  log "shadow_pid=${shadow_pid}"

  "${LAUNCH[@]}" "$PY" validation/ping_okx_bybit_2h.py \
    --duration-sec "$DURATION_SEC" \
    --okx-inst XRP-USDT-SWAP \
    --bybit-symbol XRPUSDT \
    --log-file "$ping_log" \
    >"${LOG_DIR}/${run_id}_ping.nohup.out" 2>&1 </dev/null &
  local ping_pid=$!
  echo "$ping_pid" >"$ping_pid_file"
  log "ping_pid=${ping_pid}"

  local start_utc
  start_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  expected_end_utc=$(date -u -d "@$(( $(date +%s) + DURATION_SEC ))" +%Y-%m-%dT%H:%M:%SZ)
  {
    echo "run_id=${run_id}"
    echo "n=${n}"
    echo "row_start=${start}"
    echo "row_end=${END_ROW}"
    echo "shadow_pid=${shadow_pid}"
    echo "ping_pid=${ping_pid}"
    echo "start_utc=${start_utc}"
    echo "expected_end_utc=${expected_end_utc}"
    echo "supervisor_unit=${SUPERVISOR_UNIT}"
    echo "duration_sec=${DURATION_SEC}"
    echo "persist_every=5000"
    echo "collect_bars=0"
    echo "kind=shadow"
  } >"$meta"
  cat >>"${root}/DO_NOT_TOUCH.md" <<EOF
Actual start UTC: ${start_utc}
Expected arm end UTC: ${expected_end_utc}
Shadow PID: ${shadow_pid}
Ping PID: ${ping_pid}
EOF

  smoke_shadow "$runtime_log" "$n"
  set_status "shadow_n${n}" "running pid_shadow=${shadow_pid} pid_ping=${ping_pid} start=${start_utc}"

  # Wait for ping window; then TERM shadow
  if ! wait_pid_gone "$ping_pid" $((DURATION_SEC + 120)); then
    log "WARN ping still alive; sending TERM"
    kill -TERM "$ping_pid" 2>/dev/null || true
    wait_pid_gone "$ping_pid" 60 || true
  fi

  local end_utc
  end_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  log "ping finished; TERMing shadow ${shadow_pid} at ${end_utc}"
  kill -TERM "$shadow_pid" 2>/dev/null || true
  wait_pid_gone "$shadow_pid" 180 || {
    log "WARN escalating KILL to shadow ${shadow_pid}"
    kill -KILL "$shadow_pid" 2>/dev/null || true
  }

  {
    echo "end_utc=${end_utc}"
    echo "status=complete"
  } >>"$meta"
  set_status "shadow_n${n}" "complete end=${end_utc}"
  log "=== ARM N=${n} COMPLETE ==="
  sleep 5
}

run_prod_arm() {
  local run_id="dose_n337_prod_${SERIES_DATE}"
  local root="${EXP_ROOT}/${run_id}"
  local ping_log="${LOG_DIR}/${run_id}_ping_dual.log"
  local ping_pid_file="${LOG_DIR}/${run_id}_ping.pid"
  local meta="${root}/arm_meta.env"
  local prod_pid runtime_log candidate
  local expected_end_utc

  mkdir -p "$root"
  write_arm_marker "$root" "$run_id" "337_prod"
  : >"$ping_log"

  set_status "prod_n337" "starting_ping_only"
  log "=== ARM N≈337 PROD (ping only; no second full-N shadow) ==="
  if ! systemctl is-active --quiet spread-collector; then
    log "ERROR production spread-collector is inactive; refusing invalid N≈337 arm"
    set_status "prod_n337" "blocked_prod_inactive"
    return 2
  fi
  prod_pid=$(systemctl show -p MainPID --value spread-collector)
  if [[ ! "$prod_pid" =~ ^[1-9][0-9]*$ ]]; then
    log "ERROR production MainPID invalid (${prod_pid}); refusing N≈337 arm"
    set_status "prod_n337" "blocked_invalid_production_pid"
    return 2
  fi
  candidate=$(python3 -c '
from pathlib import Path
files = [path for path in Path("/data/live/base_coin=XRP").rglob("*.parquet") if path.is_file()]
if files:
    print(max(files, key=lambda path: path.stat().st_mtime))
' || true)
  if [[ -z "$candidate" || ! -r "$candidate" || ! -w /data/live ]]; then
    log "ERROR production XRP lean artifact is not readable or /data/live is not writable; refusing invalid N≈337 arm"
    set_status "prod_n337" "blocked_no_readable_prod_xrp"
    return 2
  fi
  runtime_log=$(systemctl show -p StandardOutput --value spread-collector 2>/dev/null || true)
  log "prod_active=active mainpid=${prod_pid} readable_xrp=${candidate} stdout=${runtime_log}"

  cd "$CODE_ROOT"
  "${LAUNCH[@]}" "$PY" validation/ping_okx_bybit_2h.py \
    --duration-sec "$DURATION_SEC" \
    --okx-inst XRP-USDT-SWAP \
    --bybit-symbol XRPUSDT \
    --log-file "$ping_log" \
    >"${LOG_DIR}/${run_id}_ping.nohup.out" 2>&1 </dev/null &
  local ping_pid=$!
  echo "$ping_pid" >"$ping_pid_file"
  local start_utc
  start_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  expected_end_utc=$(date -u -d "@$(( $(date +%s) + DURATION_SEC ))" +%Y-%m-%dT%H:%M:%SZ)
  {
    echo "run_id=${run_id}"
    echo "n=337"
    echo "kind=prod_xrp_vs_ping"
    echo "ping_pid=${ping_pid}"
    echo "production_pid=${prod_pid}"
    echo "start_utc=${start_utc}"
    echo "expected_end_utc=${expected_end_utc}"
    echo "supervisor_unit=${SUPERVISOR_UNIT}"
    echo "duration_sec=${DURATION_SEC}"
    echo "s_source=${candidate}"
    echo "note=prod_bars_on_persist_default"
  } >"$meta"
  cat >>"${root}/DO_NOT_TOUCH.md" <<EOF
Actual start UTC: ${start_utc}
Expected arm end UTC: ${expected_end_utc}
Production PID: ${prod_pid}
Ping PID: ${ping_pid}
EOF
  set_status "prod_n337" "running pid_ping=${ping_pid} start=${start_utc}"
  log "ping_pid=${ping_pid} start=${start_utc}"

  if ! wait_pid_gone "$ping_pid" $((DURATION_SEC + 120)); then
    kill -TERM "$ping_pid" 2>/dev/null || true
    wait_pid_gone "$ping_pid" 60 || true
  fi
  local end_utc
  end_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  {
    echo "end_utc=${end_utc}"
    echo "status=complete"
  } >>"$meta"
  set_status "prod_n337" "complete end=${end_utc}"
  log "=== ARM N≈337 PROD COMPLETE ==="
}

main() {
  # Ignore hangup; children already in new sessions via setsid
  trap '' HUP
  echo $$ >"${LOG_DIR}/dose_n_supervisor_${SERIES_DATE}.pid"
  write_owned
  verify_chrony
  log "supervisor start series=${SERIES_DATE} duration=${DURATION_SEC} code=${CODE_ROOT}"
  log "free_mem=$(free -h | awk '/Mem:/ {print $7}') load=$(cut -d' ' -f1-3 /proc/loadavg)"
  set_status "init" "chrony_ok"

  for n in "${SHADOW_NS[@]}"; do
    run_shadow_arm "$n"
  done
  run_prod_arm

  cat >"$OWNED" <<EOF
DOSE_N_LATENCY_OWNED
owner=track_D_latency_dose_n
status=finished
series_date=${SERIES_DATE}
host=$(hostname)
finished_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)
do_not_touch=/data/experiments/dose_n*_*/
EOF
  set_status "finished" "all_arms_complete"
  log "supervisor FINISHED all arms"
}

main "$@"
