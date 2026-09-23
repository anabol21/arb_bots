#!/usr/bin/env bash
# Durable B↔C supervisor. Standalone shadow telemetry only; never writes market data.
# Probe receive_mode=immediate_drain_unbounded_app_queue (both arms). Unchanged:
# batch 30/3s, retry 10s, omitted max_queue, ping_interval/timeout 20/20.
set -euo pipefail

RUN_ID="${1:?run id required}"
ROOT="${2:?experiment root required}"
CODE_ROOT="${3:?code root required}"
PYTHON="${PYTHON:-/root/venv/bin/python}"
DURATION_SEC="${DURATION_SEC:-3600}"
WARMUP_SEC="${WARMUP_SEC:-600}"
ORDER_SEED="${ORDER_SEED:-${RUN_ID}_order}"
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

ORDER="$("$PYTHON" - "$ORDER_SEED" <<'PY'
import random
import sys
arms = ["B", "C"]
random.Random(sys.argv[1]).shuffle(arms)
print(" ".join(arms))
PY
)"

cat >"$marker" <<EOF
DO_NOT_TOUCH: active Track (D) standalone controlled B↔C latency experiment
run_id=$RUN_ID
owner=Runtime/Validation implementer
allowed_paths=$ROOT and /var/log/spread/DO_NOT_TOUCH_WSFANOUT_${RUN_ID}.txt
prohibited=kill,restart,truncate,rotate,delete,compact,backup,reclaim this experiment; other Track D work is read-only until exact series end; do not touch production collector, /data/live, /data/spool, production logs, mount, or production units
EOF
cp "$marker" "/var/log/spread/DO_NOT_TOUCH_WSFANOUT_${RUN_ID}.txt"

snapshot_background() {
  "$PYTHON" - <<'PY'
import json
import subprocess

def output(*command):
    try:
        return subprocess.check_output(command, text=True, stderr=subprocess.STDOUT).strip()
    except Exception as exc:
        return f"unavailable: {exc!r}"

units = (
    "spread-collector.service",
    "spread-compactor.service",
    "spread-compactor.timer",
    "spread-backup-transfer.service",
    "spread-backup-transfer.timer",
    "spread-bars-backup-transfer.service",
    "spread-bars-backup-transfer.timer",
)
print(json.dumps({
    "units": {
        unit: {
            "active_state": output("systemctl", "is-active", unit),
            "main_pid": output("systemctl", "show", unit, "-p", "MainPID", "--value"),
        } for unit in units
    },
    "loadavg": output("cat", "/proc/loadavg"),
    "clock": {
        "date_utc": output("date", "-u", "+%FT%TZ"),
        "ntp_synchronized": output("timedatectl", "show", "-p", "NTPSynchronized", "--value"),
        "chronyc_tracking": output("chronyc", "tracking"),
    },
    "top_processes": output("ps", "-eo", "pid,ppid,etimes,pcpu,pmem,comm,args", "--sort=-pcpu").splitlines()[:25],
}, sort_keys=True))
PY
}

"$PYTHON" - "$manifest" "$RUN_ID" "$ROOT" "$CODE_ROOT" "$DURATION_SEC" "$WARMUP_SEC" "$ORDER_SEED" "$ORDER" "$(snapshot_background)" <<'PY'
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

path, run_id, root, code_root, duration, warmup, seed, order, background = sys.argv[1:]
created = datetime.now(timezone.utc)
def output(*command):
    try:
        return subprocess.check_output(command, text=True, stderr=subprocess.STDOUT).strip()
    except Exception as exc:
        return f"unavailable: {exc!r}"
payload = {
    "run_id": run_id,
    "kind": "standalone_controlled_bc",
    "created_utc": created.isoformat(timespec="seconds").replace("+00:00", "Z"),
    "series_expected_end_utc": (created + timedelta(seconds=2 * int(duration))).isoformat(timespec="seconds").replace("+00:00", "Z"),
    "host": output("hostname", "-f"),
    "order_seed": seed,
    "order": order.split(),
    "duration_sec": int(duration),
    "warmup_sec": int(warmup),
    "steady_sec": int(duration) - int(warmup),
    "arms": {
        arm: {
            "pair_count": 300,
            "xrp_required": True,
            "expected_connections": 600,
            "expected_subscription_sends": 600,
            "non_xrp_handling": "raw_drain_discard" if arm == "B" else "full_in_memory_decode_quote_spread",
        } for arm in ("B", "C")
    },
    "controlled_factors": {
        "host": "same VPS, sequential durable series",
        "universe": "same immutable universe_300.json including XRP",
        "websocket_library": output("/root/venv/bin/python", "-c", "import websockets; print(websockets.__version__)"),
        "subscription_batch_pairs": 30,
        "subscription_batch_pause_sec": 3,
        "retry_delay_sec": 10,
        "max_queue": "omitted; installed websockets default",
        "receive_mode": "immediate_drain_unbounded_app_queue",
        "duration_sec": int(duration),
        "warmup_sec": int(warmup),
        "telemetry": "raw XRP delivery, raw loop lag, runtime/reconnect/resource metrics, fresh matched XRP ping per arm",
        "resource_limits": {"max_load_1": 8, "min_available_mib": 4096, "max_rss_mib": 2048, "max_fds": 4000, "max_cpu_percent": 95},
        "background_policy": "recorded before/after each arm; not toggled by this series",
    },
    "sole_differing_factor": "B drains/discards non-XRP frames; C performs full in-memory JSON decode, quote update, and spread calculation for non-XRP frames.",
    "validity_gates": {
        "subscriptions_before_steady": "600/600 connection opens and subscription sends",
        "max_unplanned_reconnects_per_exchange": 1,
        "max_unrecovered_connections": 0,
        "max_reconnect_wave_events_per_exchange_60s": 3,
        "raw_and_ping_coverage": "complete after warmup with overlap",
        "safety_abort": 0,
        "failure_verdict": "measurement_failed; no causal inference if either arm fails",
    },
    "background_before_series": json.loads(background),
    "code": {
        "root": code_root,
        "git_head": output("git", "-C", code_root, "rev-parse", "HEAD"),
        "python": output("/root/venv/bin/python", "--version"),
        "websockets": output("/root/venv/bin/python", "-c", "import websockets; print(websockets.__version__)"),
    },
    "persistence": "disabled_no_parquet_no_spool_no_publisher_no_bars",
}
Path(path).write_text(json.dumps(payload, indent=2) + "\n")
PY

preflight() {
  test "$(systemctl is-active spread-collector)" = active
  test "$(timedatectl show -p NTPSynchronized --value)" = yes
  chronyc tracking | grep -q "Leap status.*Normal"
  available_kib="$(awk '/MemAvailable:/ {print $2}' /proc/meminfo)"
  test "$available_kib" -ge "$((MIN_AVAILABLE_MIB * 1024))"
  load_1="$(awk '{print $1}' /proc/loadavg)"
  awk -v current="$load_1" -v maximum="$MAX_LOAD_1" 'BEGIN { exit !(current <= maximum) }'
  established="$(ss -s | awk '/^TCP:/ {for (i=1; i<=NF; i++) if ($i ~ /^estab$/) {value=$(i+1); gsub(/[^0-9]/, "", value); print value; exit}}')"
  test "${established:-0}" -lt "$MAX_ESTABLISHED_TCP"
}

for arm in $ORDER; do
  preflight
  arm_root="$ROOT/arm_$arm"
  mkdir -p "$arm_root"
  arm_start="$(date -u +%FT%TZ)"
  background_before_arm="$(snapshot_background)"
  "$PYTHON" - "$manifest" "$arm_root/arm_manifest.json" "$arm" "$arm_start" "$background_before_arm" <<'PY'
import json
import sys
from pathlib import Path

run, target, arm, start, background = sys.argv[1:]
source = json.loads(Path(run).read_text())
Path(target).write_text(json.dumps({
    "run_id": source["run_id"], "arm": arm, "start_utc": start,
    "order": source["order"], "order_seed": source["order_seed"],
    "expected": source["arms"][arm], "controlled_factors": source["controlled_factors"],
    "sole_differing_factor": source["sole_differing_factor"],
    "validity_gates": source["validity_gates"],
    "background_before_arm": json.loads(background),
    "artifacts": {"runtime": "runtime.jsonl", "ping": "ping_xrp.log",
                  "xrp_okx_samples": "xrp_delivery_okx.csv",
                  "xrp_bybit_samples": "xrp_delivery_bybit.csv", "loop_lag_samples": "loop_lag.csv"},
}, indent=2) + "\n")
PY
  printf '%s arm_start arm=%s\n' "$arm_start" "$arm" | tee -a "$status"
  "$PYTHON" "$CODE_ROOT/ws_fanout_matched_ping.py" \
    --duration-sec "$DURATION_SEC" --retry-delay-sec "$RETRY_DELAY_SEC" \
    --log-file "$arm_root/ping_xrp.log" >"$arm_root/ping_stdout.log" 2>&1 &
  ping_pid=$!
  "$PYTHON" "$CODE_ROOT/ws_fanout_three_arm.py" \
    --arm "$arm" --run-id "$RUN_ID" --duration-sec "$DURATION_SEC" \
    --universe-csv "$UNIVERSE_CSV" --manifest "$ROOT/universe_300.json" \
    --log-file "$arm_root/runtime.jsonl" --pair-count 300 \
    --subscription-batch-pairs "$SUBSCRIPTION_BATCH_PAIRS" \
    --subscription-batch-pause-sec "$SUBSCRIPTION_BATCH_PAUSE_SEC" \
    --retry-delay-sec "$RETRY_DELAY_SEC" \
    --max-rss-mib "$MAX_RSS_MIB" --max-load-1 "$MAX_LOAD_1" \
    --min-mem-available-mib "$MIN_AVAILABLE_MIB" --max-fds "$MAX_FDS" \
    --max-cpu-percent "$MAX_CPU_PERCENT" \
    --xrp-okx-samples "$arm_root/xrp_delivery_okx.csv" \
    --xrp-bybit-samples "$arm_root/xrp_delivery_bybit.csv" \
    --loop-lag-samples "$arm_root/loop_lag.csv" >"$arm_root/probe_stdout.log" 2>&1 &
  probe_pid=$!
  printf 'arm=%s probe_pid=%s ping_pid=%s start_utc=%s\n' \
    "$arm" "$probe_pid" "$ping_pid" "$(date -u +%FT%TZ)" | tee "$arm_root/pids.env" | tee -a "$status"
  if wait "$probe_pid"; then probe_status=0; else probe_status=$?; fi
  if wait "$ping_pid"; then ping_status=0; else ping_status=$?; fi
  background_after_arm="$(snapshot_background)"
  "$PYTHON" - "$arm_root/arm_manifest.json" "$probe_status" "$ping_status" "$background_after_arm" <<'PY'
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

path, probe_status, ping_status, background = sys.argv[1:]
payload = json.loads(Path(path).read_text())
payload.update({
    "end_utc": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
    "probe_exit_status": int(probe_status), "ping_exit_status": int(ping_status),
    "background_after_arm": json.loads(background),
})
Path(path).write_text(json.dumps(payload, indent=2) + "\n")
PY
  printf 'arm=%s probe_status=%s ping_status=%s end_utc=%s\n' \
    "$arm" "$probe_status" "$ping_status" "$(date -u +%FT%TZ)" | tee -a "$status"
  test "$probe_status" -eq 0
  test "$ping_status" -eq 0
  "$PYTHON" -c 'import pathlib, sys; raise SystemExit(1 if any("\"event\":\"safety_abort\"" in line for line in pathlib.Path(sys.argv[1]).open()) else 0)' "$arm_root/runtime.jsonl"
done

printf '%s supervisor_finished run_id=%s\n' "$(date -u +%FT%TZ)" "$RUN_ID" | tee -a "$status"
