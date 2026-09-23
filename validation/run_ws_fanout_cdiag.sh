#!/usr/bin/env bash
# Durable supervisor payload for the standalone C reconnect-diagnostics repeat.
set -euo pipefail

RUN_ID="${1:?run id required}"
ROOT="${2:?experiment root required}"
CODE_ROOT="${3:?code root required}"
PYTHON="${PYTHON:-/root/venv/bin/python}"
DURATION_SEC="${DURATION_SEC:-3600}"
WARMUP_SEC="${WARMUP_SEC:-600}"
UNIVERSE_CSV="${UNIVERSE_CSV:-$CODE_ROOT/bybit_okx_universe.csv}"
MAX_ESTABLISHED_TCP="${MAX_ESTABLISHED_TCP:-1800}"
MIN_AVAILABLE_MIB="${MIN_AVAILABLE_MIB:-4096}"
MAX_LOAD_1="${MAX_LOAD_1:-8}"
MAX_RSS_MIB="${MAX_RSS_MIB:-2048}"
MAX_FDS="${MAX_FDS:-4000}"
MAX_CPU_PERCENT="${MAX_CPU_PERCENT:-95}"
SUBSCRIPTION_BATCH_PAIRS=30
SUBSCRIPTION_BATCH_PAUSE_SEC=3
RETRY_DELAY_SEC=10

mkdir -p "$ROOT"
status="$ROOT/supervisor.status"
marker="$ROOT/DO_NOT_TOUCH.md"
manifest="$ROOT/run_manifest.json"
arm_root="$ROOT/arm_C"

cat >"$marker" <<EOF
DO_NOT_TOUCH: active Track (D) standalone C reconnect diagnostic
run_id=$RUN_ID
owner=Runtime/Validation implementer
allowed_paths=$ROOT and /var/log/spread/DO_NOT_TOUCH_WSFANOUT_${RUN_ID}.txt
prohibited=kill,restart,truncate,rotate,delete,compact,backup,reclaim this experiment; other Track D branches are read-only until exact_end_utc; do not touch production collector, /data/live, /data/spool, production logs, mount, or unit
EOF
cp "$marker" "/var/log/spread/DO_NOT_TOUCH_WSFANOUT_${RUN_ID}.txt"

"$PYTHON" - "$manifest" "$RUN_ID" "$ROOT" "$DURATION_SEC" "$WARMUP_SEC" "$CODE_ROOT" <<'PY'
import json, subprocess, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

path, run_id, root, duration, warmup, code_root = sys.argv[1:]
created = datetime.now(timezone.utc)
def output(*command):
    try:
        return subprocess.check_output(command, text=True, stderr=subprocess.STDOUT).strip()
    except Exception as exc:
        return f"unavailable: {exc!r}"
payload = {
    "run_id": run_id,
    "kind": "standalone_C_reconnect_diagnostic",
    "created_utc": created.isoformat(timespec="seconds").replace("+00:00", "Z"),
    "exact_end_utc": (created + timedelta(seconds=int(duration))).isoformat(timespec="seconds").replace("+00:00", "Z"),
    "host": output("hostname", "-f"),
    "duration_sec": int(duration), "warmup_sec": int(warmup), "steady_sec": int(duration) - int(warmup),
    "arm": "C", "pair_count": 300, "xrp_required": True,
    "expected_connections": 600, "expected_subscription_sends": 600,
    "scope": "full_in_memory_decode_quote_spread; no_parquet_no_publisher_no_spool_no_bars",
    "connection_parameters": {
        "subscription_batch_pairs": 30, "subscription_batch_pause_sec": 3,
        "retry_delay_sec": 10, "max_queue": None,
        "max_queue_rationale": "The standalone probe omits max_queue so websockets uses its installed-library default, matching the production source behavior more closely than r2's explicit unbounded queue. The installed version/default are recorded; this avoids silently changing receive-backpressure semantics.",
    },
    "reconnect_validity_contract": {
        "max_unplanned_reconnects_per_exchange": 1,
        "max_unrecovered_connections": 0,
        "max_connection_wave_events_per_exchange_60s": 3,
        "failure_verdict": "measurement_failed",
    },
    "clock": {"date_utc": output("date", "-u", "+%FT%TZ"), "ntp_synchronized": output("timedatectl", "show", "-p", "NTPSynchronized", "--value"), "chronyc_tracking": output("chronyc", "tracking")},
    "production_state": {"service": output("systemctl", "is-active", "spread-collector"), "main_pid": output("systemctl", "show", "spread-collector", "-p", "MainPID", "--value"), "unit": output("systemctl", "show", "spread-collector", "-p", "ActiveEnterTimestamp", "-p", "LimitNOFILE")},
    "code": {"root": code_root, "git_head": output("git", "-C", code_root, "rev-parse", "HEAD"), "python": output("/root/venv/bin/python", "--version"), "websockets": output("/root/venv/bin/python", "-c", "import websockets; print(websockets.__version__)")},
    "artifacts": {"runtime": "arm_C/runtime.jsonl", "ping": "arm_C/ping_xrp.log", "raw_delivery": ["arm_C/xrp_delivery_okx.csv", "arm_C/xrp_delivery_bybit.csv"], "loop_lag": "arm_C/loop_lag.csv"},
}
Path(path).write_text(json.dumps(payload, indent=2) + "\n")
PY

preflight() {
  test "$(systemctl is-active spread-collector)" = active
  test "$(timedatectl show -p NTPSynchronized --value)" = yes
  available_kib="$(awk '/MemAvailable:/ {print $2}' /proc/meminfo)"
  test "$available_kib" -ge "$((MIN_AVAILABLE_MIB * 1024))"
  load_1="$(awk '{print $1}' /proc/loadavg)"
  awk -v current="$load_1" -v maximum="$MAX_LOAD_1" 'BEGIN { exit !(current <= maximum) }'
  established="$(ss -s | awk '/^TCP:/ {for (i=1; i<=NF; i++) if ($i ~ /^estab$/) {value=$(i+1); gsub(/[^0-9]/, "", value); print value; exit}}')"
  test "${established:-0}" -lt "$MAX_ESTABLISHED_TCP"
}

preflight
mkdir -p "$arm_root"
printf '%s supervisor_start run_id=%s exact_end_utc=%s\n' "$(date -u +%FT%TZ)" "$RUN_ID" "$("$PYTHON" -c "import json; print(json.load(open('$manifest'))['exact_end_utc'])")" | tee -a "$status"
"$PYTHON" "$CODE_ROOT/ws_fanout_matched_ping.py" --duration-sec "$DURATION_SEC" --retry-delay-sec "$RETRY_DELAY_SEC" --log-file "$arm_root/ping_xrp.log" >"$arm_root/ping_stdout.log" 2>&1 &
ping_pid=$!
"$PYTHON" "$CODE_ROOT/ws_fanout_three_arm.py" --arm C --run-id "$RUN_ID" --duration-sec "$DURATION_SEC" --universe-csv "$UNIVERSE_CSV" --manifest "$ROOT/universe_300.json" --log-file "$arm_root/runtime.jsonl" --pair-count 300 --subscription-batch-pairs "$SUBSCRIPTION_BATCH_PAIRS" --subscription-batch-pause-sec "$SUBSCRIPTION_BATCH_PAUSE_SEC" --retry-delay-sec "$RETRY_DELAY_SEC" --max-rss-mib "$MAX_RSS_MIB" --max-load-1 "$MAX_LOAD_1" --min-mem-available-mib "$MIN_AVAILABLE_MIB" --max-fds "$MAX_FDS" --max-cpu-percent "$MAX_CPU_PERCENT" --xrp-okx-samples "$arm_root/xrp_delivery_okx.csv" --xrp-bybit-samples "$arm_root/xrp_delivery_bybit.csv" --loop-lag-samples "$arm_root/loop_lag.csv" >"$arm_root/probe_stdout.log" 2>&1 &
probe_pid=$!
printf 'arm=C probe_pid=%s ping_pid=%s start_utc=%s\n' "$probe_pid" "$ping_pid" "$(date -u +%FT%TZ)" | tee "$arm_root/pids.env" | tee -a "$status"
if wait "$probe_pid"; then probe_status=0; else probe_status=$?; fi
if wait "$ping_pid"; then ping_status=0; else ping_status=$?; fi
printf 'arm=C probe_status=%s ping_status=%s end_utc=%s\n' "$probe_status" "$ping_status" "$(date -u +%FT%TZ)" | tee -a "$status"
test "$probe_status" -eq 0
test "$ping_status" -eq 0
"$PYTHON" -c 'import pathlib, sys; raise SystemExit(1 if any("\"event\":\"safety_abort\"" in line for line in pathlib.Path(sys.argv[1]).open()) else 0)' "$arm_root/runtime.jsonl"
