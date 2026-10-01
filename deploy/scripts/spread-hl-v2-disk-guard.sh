#!/bin/bash
# Fail-closed HL v2 disk guard. Stop only spread-collector-hl-v2.
set -euo pipefail
FREE_G=$(df -BG /data 2>/dev/null | awk 'NR==2{gsub(/G/,"",$4); print $4}')
if [ -z "${FREE_G:-}" ]; then
  FREE_G=$(df -BG / | awk 'NR==2{gsub(/G/,"",$4); print $4}')
fi
bytes=0
for d in /data/live_hl_v2 /data/spool_hl_v2 /data/gaps_hl_v2 /data/compacted_hl_v2; do
  if [ -d "$d" ]; then
    b=$(du -sb "$d" 2>/dev/null | awk '{print $1}')
    bytes=$((bytes + ${b:-0}))
  fi
done
HL_G=$(( (bytes + 1073741823) / 1073741824 ))
printf '%s\n' "{\"event\":\"hl_v2_disk_guard\",\"free_g\":${FREE_G},\"hl_sum_g\":${HL_G}}"
if [ "$FREE_G" -lt 15 ] || [ "$HL_G" -ge 4 ]; then
  systemctl stop spread-collector-hl-v2.service || true
  printf '%s\n' "{\"event\":\"hl_v2_disk_guard_stop\",\"reason\":\"budget\",\"free_g\":${FREE_G},\"hl_sum_g\":${HL_G}}"
fi
