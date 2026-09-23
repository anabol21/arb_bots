#!/bin/bash
set -euo pipefail
EXP=/data/experiments/throughput_20260729
mkdir -p "$EXP/local" "$EXP/logs"

HANG=$(ps -eo pid=,args= | awk '$2 ~ /(^|\/)rclone$/ || $0 ~ /-m app\.storage\.backup_transfer/ {print}')
if [ -n "$HANG" ]; then
  echo REFUSING_HANGING_TRANSFER
  printf '%s\n' "$HANG"
  exit 2
fi

nohup setsid /root/venv/bin/python /root/spread_staging/validation/throughput_benchmark.py \
  --local-dir "$EXP/local" \
  --remote backup1tb \
  --remote-prefix throughput-20260729 \
  --key-path /root/.ssh/id_ed25519_uploader \
  --lock-path /run/spread-backup.lock \
  --log-path "$EXP/logs/throughput.jsonl" \
  --series-count 3 \
  --gap-between-files-s 150 \
  --gap-between-series-s 7200 \
  --timeout-s 600 \
  --download-size-mib 20 \
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
  tail -n 20 "$EXP/logs/console.log" || true
fi
