#!/usr/bin/env bash
# Durable remote supervisor payload for the isolated ws fan-out experiment.
set -euo pipefail

RUN_ID="${1:?run id required}"
ROOT="${2:?experiment root required}"
CODE_ROOT="${3:?code root required}"
DURATION_SEC="${DURATION_SEC:-3600}"
ORDER="${ORDER:-A B C}"
ORDER_SEED="${ORDER_SEED:-wsfanout_abc_20260812r2}"
PYTHON="${PYTHON:-/root/venv/bin/python}"
UNIVERSE_CSV="${UNIVERSE_CSV:-$CODE_ROOT/bybit_okx_universe.csv}"
SOURCE_GIT_HEAD="${SOURCE_GIT_HEAD:-unavailable}"
export SOURCE_GIT_HEAD MAX_LOAD_1 MIN_AVAILABLE_MIB MAX_RSS_MIB MAX_FDS MAX_CPU_PERCENT PYTHON
MAX_ESTABLISHED_TCP="${MAX_ESTABLISHED_TCP:-1800}"
MIN_AVAILABLE_MIB="${MIN_AVAILABLE_MIB:-4096}"
MAX_LOAD_1="${MAX_LOAD_1:-8}"
MAX_RSS_MIB="${MAX_RSS_MIB:-2048}"
MAX_FDS="${MAX_FDS:-4000}"
MAX_CPU_PERCENT="${MAX_CPU_PERCENT:-95}"

status="$ROOT/supervisor.status"
marker="$ROOT/DO_NOT_TOUCH.md"
manifest="$ROOT/run_manifest.json"
mkdir -p "$ROOT"

cat >"$marker" <<EOF
DO_NOT_TOUCH: active Track (D) ws fan-out experiment
run_id=$RUN_ID
owner=Runtime Storage + Validation
allowed_paths=$ROOT and /var/log/spread/DO_NOT_TOUCH_WSFANOUT_${RUN_ID}.txt
prohibited=kill,restart,truncate,rotate,delete,compact,reclaim this experiment; do not touch production collector, /data/live, /data/spool, or production logs
EOF
cp "$marker" "/var/log/spread/DO_NOT_TOUCH_WSFANOUT_${RUN_ID}.txt"

write_manifest() {
  "$PYTHON" - "$manifest" "$RUN_ID" "$ORDER" "$ORDER_SEED" "$CODE_ROOT" "$DURATION_SEC" <<'PY'
import json, os, subprocess, sys
from datetime import datetime, timezone
from pathlib import Path

path, run_id, order, seed, code_root, duration = map(str, sys.argv[1:])
def output(*command):
    try:
        return subprocess.check_output(command, text=True, stderr=subprocess.STDOUT).strip()
    except Exception as exc:
        return f"unavailable: {exc!r}"
prod_pid = output("systemctl", "show", "spread-collector", "-p", "MainPID", "--value")
payload = {
    "run_id": run_id,
    "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
    "host": output("hostname", "-f"),
    "clock": {
        "date_utc": output("date", "-u", "+%FT%TZ"),
        "ntp_synchronized": output("timedatectl", "show", "-p", "NTPSynchronized", "--value"),
        "chronyc_tracking": output("chronyc", "tracking"),
    },
    "order": order.split(),
    "order_seed": seed,
    "duration_sec": int(duration),
    "warmup_sec": 600,
    "steady_sec": 3000,
    "arms": {
        arm: {"pair_count": 1 if arm == "A" else 300, "expected_connections": 2 if arm == "A" else 600,
              "expected_subscription_sends": 2 if arm == "A" else 600}
        for arm in "ABC"
    },
    "code": {"root": code_root, "git_head": output("git", "-C", code_root, "rev-parse", "HEAD"),
             "git_status": output("git", "-C", code_root, "status", "--short"),
             "source_git_head": os.environ.get("SOURCE_GIT_HEAD", "unavailable"),
             "python": output(os.environ.get("PYTHON", "/root/venv/bin/python"), "--version"),
             "websockets": output(os.environ.get("PYTHON", "/root/venv/bin/python"), "-c", "import websockets; print(websockets.__version__)")},
    "production_state": {"service": output("systemctl", "is-active", "spread-collector"), "main_pid": prod_pid,
                         "unit": output("systemctl", "show", "spread-collector", "-p", "ActiveEnterTimestamp", "-p", "LimitNOFILE")},
    "background_process_summary": output("ps", "-eo", "pid,ppid,etimes,pcpu,pmem,comm,args", "--sort=-pcpu"),
    "resource_abort_budgets": {"max_load_1": float(os.environ.get("MAX_LOAD_1", "8")),
                               "min_available_mib": int(os.environ.get("MIN_AVAILABLE_MIB", "4096")),
                               "max_rss_mib": int(os.environ.get("MAX_RSS_MIB", "2048")),
                               "max_fds": int(os.environ.get("MAX_FDS", "4000")),
                               "max_cpu_percent": float(os.environ.get("MAX_CPU_PERCENT", "95"))},
}
payload["background_process_summary"] = "\n".join(payload["background_process_summary"].splitlines()[:25])
Path(path).write_text(json.dumps(payload, indent=2) + "\n")
PY
}

write_manifest
echo "$(date -u +%FT%TZ) supervisor_start run_id=$RUN_ID order=$ORDER order_seed=$ORDER_SEED duration_sec=$DURATION_SEC manifest=$manifest" | tee -a "$status"

preflight() {
  test "$(systemctl is-active spread-collector)" = active
  available_kib="$(awk '/MemAvailable:/ {print $2}' /proc/meminfo)"
  test "$available_kib" -ge "$((MIN_AVAILABLE_MIB * 1024))"
  load_1="$(awk '{print $1}' /proc/loadavg)"
  awk -v current_load="$load_1" -v max="$MAX_LOAD_1" 'BEGIN { exit !(current_load <= max) }'
  established="$(ss -s | awk '/^TCP:/ {for (i=1; i<=NF; i++) if ($i ~ /^estab$/) {value=$(i+1); gsub(/[^0-9]/, "", value); print value; exit}}')"
  test "${established:-0}" -lt "$MAX_ESTABLISHED_TCP"
  test "$(timedatectl show -p NTPSynchronized --value)" = yes
}

for arm in $ORDER; do
  preflight
  arm_root="$ROOT/arm_$arm"
  mkdir -p "$arm_root"
  arm_start="$(date -u +%FT%TZ)"
  "$PYTHON" - "$manifest" "$arm_root/arm_manifest.json" "$arm" "$arm_start" <<'PY'
import json, sys
from pathlib import Path

run, target, arm, start = sys.argv[1:]
source = json.loads(Path(run).read_text())
Path(target).write_text(json.dumps({
    "run_id": source["run_id"], "arm": arm, "start_utc": start,
    "expected": source["arms"][arm], "order": source["order"],
    "order_seed": source["order_seed"], "host": source["host"], "clock": source["clock"],
    "code": source["code"], "production_state": source["production_state"],
    "background_process_summary": source["background_process_summary"],
    "resource_abort_budgets": source["resource_abort_budgets"],
    "artifacts": {"runtime": "runtime.jsonl", "ping": "ping_xrp.log",
                  "xrp_okx_samples": "xrp_delivery_okx.csv",
                  "xrp_bybit_samples": "xrp_delivery_bybit.csv", "loop_lag_samples": "loop_lag.csv"},
    "measurement_failure_gate": "any exchange >1 unplanned_reconnect or any unrecovered connection",
}, indent=2) + "\n")
PY
  echo "$(date -u +%FT%TZ) arm_start arm=$arm" | tee -a "$status"
  "$PYTHON" "$CODE_ROOT/ws_fanout_matched_ping.py" \
    --duration-sec "$DURATION_SEC" \
    --log-file "$arm_root/ping_xrp.log" >"$arm_root/ping_stdout.log" 2>&1 &
  ping_pid=$!
  "$PYTHON" "$CODE_ROOT/ws_fanout_three_arm.py" \
    --arm "$arm" \
    --run-id "$RUN_ID" \
    --duration-sec "$DURATION_SEC" \
    --universe-csv "$UNIVERSE_CSV" \
    --manifest "$ROOT/universe_300.json" \
    --log-file "$arm_root/runtime.jsonl" \
    --pair-count 300 \
    --connect-interval-sec 0.05 \
    --max-rss-mib "$MAX_RSS_MIB" \
    --max-load-1 "$MAX_LOAD_1" \
    --min-mem-available-mib "$MIN_AVAILABLE_MIB" \
    --max-fds "$MAX_FDS" \
    --max-cpu-percent "$MAX_CPU_PERCENT" \
    --xrp-okx-samples "$arm_root/xrp_delivery_okx.csv" \
    --xrp-bybit-samples "$arm_root/xrp_delivery_bybit.csv" \
    --loop-lag-samples "$arm_root/loop_lag.csv" >"$arm_root/probe_stdout.log" 2>&1 &
  probe_pid=$!
  printf 'arm=%s probe_pid=%s ping_pid=%s start_utc=%s\n' \
    "$arm" "$probe_pid" "$ping_pid" "$(date -u +%FT%TZ)" | tee "$arm_root/pids.env" | tee -a "$status"
  if wait "$probe_pid"; then probe_status=0; else probe_status=$?; fi
  if wait "$ping_pid"; then ping_status=0; else ping_status=$?; fi
  printf 'arm=%s probe_status=%s ping_status=%s end_utc=%s\n' \
    "$arm" "$probe_status" "$ping_status" "$(date -u +%FT%TZ)" | tee -a "$status"
  "$PYTHON" - "$arm_root/arm_manifest.json" "$probe_status" "$ping_status" <<'PY'
import json, sys
from datetime import datetime, timezone
from pathlib import Path

path, probe_status, ping_status = sys.argv[1:]
payload = json.loads(Path(path).read_text())
payload.update({
    "end_utc": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
    "probe_exit_status": int(probe_status), "ping_exit_status": int(ping_status),
})
Path(path).write_text(json.dumps(payload, indent=2) + "\n")
PY
  test "$probe_status" -eq 0
  test "$ping_status" -eq 0
  "$PYTHON" -c 'import pathlib, sys; raise SystemExit(1 if any("\"event\":\"safety_abort\"" in line for line in pathlib.Path(sys.argv[1]).open()) else 0)' "$arm_root/runtime.jsonl"
done

echo "$(date -u +%FT%TZ) supervisor_finished run_id=$RUN_ID" | tee -a "$status"
