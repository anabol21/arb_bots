#!/bin/bash
# Full series with best WORKING tuned flags discovered by probe_sftp_flag_matrix.sh.
# Support-exact (concurrency 32 / chunk 512k) failed with EOF — do not use those here.
set -euo pipefail
EXP=/data/experiments/throughput_20260730_tuned
RCLONE_BIN=${RCLONE_BIN:-/opt/rclone-1.74.4/rclone}
PREFIX=${REMOTE_PREFIX:-throughput-20260730-tuned/full_conc8_128k}
mkdir -p "$EXP/local" "$EXP/logs"

HANG=$(ps -eo pid=,args= | awk '
  $0 ~ /awk/ { next }
  $2 ~ /(^|\/)rclone$/ || $0 ~ /-m app\.storage\.backup_transfer/ || $0 ~ /throughput_benchmark\.py/ || $0 ~ /probe_sftp_flag_matrix\.sh/ { print }
')
if [ -n "$HANG" ]; then
  echo REFUSING_HANGING_TRANSFER
  printf '%s\n' "$HANG"
  exit 2
fi

nohup setsid /root/venv/bin/python /root/spread_staging/validation/throughput_benchmark.py \
  --local-dir "$EXP/local" \
  --remote backup1tb \
  --remote-prefix "$PREFIX" \
  --key-path /root/.ssh/id_ed25519_uploader \
  --rclone "$RCLONE_BIN" \
  --lock-path /run/spread-backup.lock \
  --log-path "$EXP/logs/throughput_full_conc8_128k.jsonl" \
  --series-count 1 \
  --gap-between-files-s 90 \
  --gap-between-series-s 0 \
  --timeout-s 600 \
  --download-size-mib 20 \
  --rclone-extra-flag=--sftp-concurrency \
  --rclone-extra-flag=8 \
  --rclone-extra-flag=--sftp-chunk-size \
  --rclone-extra-flag=128k \
  </dev/null >"$EXP/logs/console_full_conc8_128k.log" 2>&1 &

echo $! > "$EXP/logs/benchmark_full_conc8_128k.pid"
sleep 3
PID=$(cat "$EXP/logs/benchmark_full_conc8_128k.pid")
ps -o pid,etime,state,args -p "$PID" || true
head -n 2 "$EXP/logs/throughput_full_conc8_128k.jsonl" 2>/dev/null || echo WAITING
