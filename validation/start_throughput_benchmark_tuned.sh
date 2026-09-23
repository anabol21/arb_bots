#!/bin/bash
# Tuned SFTP upload benchmark using support-recommended rclone flags.
# Uses side-by-side rclone 1.74.4 so system /usr/bin/rclone (1.53.3) stays untouched.
set -euo pipefail
EXP=/data/experiments/throughput_20260730_tuned
RCLONE_BIN=${RCLONE_BIN:-/opt/rclone-1.74.4/rclone}
mkdir -p "$EXP/local" "$EXP/logs"

HANG=$(ps -eo pid=,args= | awk '$2 ~ /(^|\/)rclone$/ || $0 ~ /-m app\.storage\.backup_transfer/ || $0 ~ /throughput_benchmark\.py/ {print}')
if [ -n "$HANG" ]; then
  echo REFUSING_HANGING_TRANSFER
  printf '%s\n' "$HANG"
  exit 2
fi

if [ ! -x "$RCLONE_BIN" ]; then
  echo "missing rclone binary: $RCLONE_BIN" >&2
  exit 3
fi

"$RCLONE_BIN" version | tee "$EXP/logs/rclone_version.txt"
"$RCLONE_BIN" help flags 2>&1 | grep -E 'sftp-concurrency|sftp-chunk-size|--transfers|--buffer-size' \
  | tee "$EXP/logs/rclone_flags_used.txt" || true

nohup setsid /root/venv/bin/python /root/spread_staging/validation/throughput_benchmark.py \
  --local-dir "$EXP/local" \
  --remote backup1tb \
  --remote-prefix throughput-20260730-tuned \
  --key-path /root/.ssh/id_ed25519_uploader \
  --rclone "$RCLONE_BIN" \
  --lock-path /run/spread-backup.lock \
  --log-path "$EXP/logs/throughput.jsonl" \
  --series-count 1 \
  --gap-between-files-s 90 \
  --gap-between-series-s 0 \
  --timeout-s 600 \
  --download-size-mib 20 \
  --rclone-extra-flag=--sftp-concurrency \
  --rclone-extra-flag=32 \
  --rclone-extra-flag=--sftp-chunk-size \
  --rclone-extra-flag=512k \
  --rclone-extra-flag=--transfers \
  --rclone-extra-flag=8 \
  --rclone-extra-flag=--buffer-size \
  --rclone-extra-flag=128M \
  </dev/null >"$EXP/logs/console.log" 2>&1 &

echo $! > "$EXP/logs/benchmark.pid"
sleep 2
PID=$(awk 'NR==1 {print; exit}' "$EXP/logs/benchmark.pid")
ps -o pid,ppid,sid,etime,state,args -p "$PID"
printf 'pid_file=%s\n' "$PID"
sleep 5
if [ -f "$EXP/logs/throughput.jsonl" ]; then
  head -n 3 "$EXP/logs/throughput.jsonl"
else
  echo WAITING_FOR_LOG
  ls -la "$EXP/local" "$EXP/logs"
  tail -n 40 "$EXP/logs/console.log" || true
fi
