#!/bin/bash
# Probe matrix for support-recommended SFTP flags vs milder variants.
# Writes JSONL samples; does not delete prior throughput-20260729 data.
set -euo pipefail
EXP=/data/experiments/throughput_20260730_tuned
R=/opt/rclone-1.74.4/rclone
KEY=/root/.ssh/id_ed25519_uploader
BASE=backup1tb:throughput-20260730-tuned/probes
LOG=$EXP/logs/probe_matrix.jsonl
mkdir -p "$EXP/probes" "$EXP/logs"
: > "$LOG"
dd if=/dev/urandom of="$EXP/probes/probe_5m.bin" bs=1M count=5 status=none conv=fsync

run_probe() {
  local name="$1"; shift
  local dest="$BASE/${name}.bin"
  local start end dur thr rc
  start=$(date +%s.%N)
  set +e
  flock /run/spread-backup.lock timeout 180 "$R" copyto "$EXP/probes/probe_5m.bin" "$dest" \
    --timeout 170s --contimeout 15s --retries 1 --sftp-key-file "$KEY" "$@" \
    >/tmp/probe_${name}.out 2>/tmp/probe_${name}.err
  rc=$?
  set -e
  end=$(date +%s.%N)
  dur=$(python3 -c "print(max($end-$start,1e-9))")
  thr=$(python3 -c "print(5.0/float('$dur'))")
  python3 - "$name" "$rc" "$dur" "$thr" "$*" <<'PY' | tee -a "$LOG"
import json,sys,time
name,rc,dur,thr,flags=sys.argv[1:6]
err=open(f"/tmp/probe_{name}.err").read()[-500:]
print(json.dumps({
  "event":"probe",
  "name":name,
  "flags":flags,
  "returncode":int(rc),
  "duration_s":round(float(dur),6),
  "throughput_mib_s":round(float(thr),6),
  "success": int(rc)==0,
  "stderr_tail":err,
  "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
}, sort_keys=True))
PY
}

{
  echo "=== probe matrix start $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
  "$R" version
  run_probe v174_defaults
  run_probe transfers8_buf128 --transfers 8 --buffer-size 128M
  run_probe conc4_chunk32k --sftp-concurrency 4 --sftp-chunk-size 32k --transfers 4 --buffer-size 16M
  run_probe conc8_chunk128k --sftp-concurrency 8 --sftp-chunk-size 128k --transfers 4 --buffer-size 32M
  run_probe conc16_chunk256k --sftp-concurrency 16 --sftp-chunk-size 256k --transfers 4 --buffer-size 64M
  run_probe support_exact --sftp-concurrency 32 --sftp-chunk-size 512k --transfers 8 --buffer-size 128M
  echo "=== remote listing ==="
  "$R" lsf "$BASE" --sftp-key-file "$KEY" --timeout 60s --retries 1 || true
  echo "=== probe matrix done $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
} >"$EXP/logs/probe_console.log" 2>&1
