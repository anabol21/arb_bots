#!/bin/bash
# Idle-wait for spread-bt-features chunk1, then run backup-gap features.
# ~0 CPU while sleeping. Do not stop collector/canary/chunk1.
set -u
BUILD=/data/experiments/gear22_bt_features_build
PY="$BUILD/run_backup_gaps.py"

log() {
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $*"
}

chunk1_busy() {
  local state pid
  state=$(systemctl show -p ActiveState --value spread-bt-features.service 2>/dev/null || echo unknown)
  pid=$(systemctl show -p MainPID --value spread-bt-features.service 2>/dev/null || echo 0)
  case "$state" in
    active|activating) return 0 ;;
  esac
  if [ -n "$pid" ] && [ "$pid" != "0" ] && kill -0 "$pid" 2>/dev/null; then
    return 0
  fi
  if pgrep -f '/data/experiments/gear22_bt_features_build/run_chunk1.py' >/dev/null 2>&1; then
    return 0
  fi
  return 1
}

log "waiter start pid=$$"
while chunk1_busy; do
  state=$(systemctl show -p ActiveState --value spread-bt-features.service 2>/dev/null || echo unknown)
  pid=$(systemctl show -p MainPID --value spread-bt-features.service 2>/dev/null || echo 0)
  log "waiting chunk1 state=$state pid=$pid (sleep 60, idle)"
  sleep 60
done

result=$(systemctl show -p Result --value spread-bt-features.service 2>/dev/null || echo unknown)
log "chunk1 inactive result=$result; launching backup gaps"
exec /usr/bin/nice -n 19 /usr/bin/ionice -c 3 /root/venv/bin/python "$PY"
